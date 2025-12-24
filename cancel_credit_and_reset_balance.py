import os
import logging
from typing import List, Dict, Set
from datetime import datetime, timezone

import pymongo
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI")
API_KEY = os.getenv("API_KEY")
BASE_URL = os.getenv("BASE_URL")

if not all([MONGO_URI, API_KEY, BASE_URL]):
    raise ValueError("یکی از متغیرهای محیطی MONGO_URI, API_KEY یا BASE_URL تنظیم نشده است.")

# دسته‌بندی انواع wallet ها - این‌ها خیلی مهم هستند و نباید تغییر کنند
MAIN_WALLET_TYPES = {"b2c", "mohlat"}
OTHER_WALLET_TYPES = {"travel", "tajan", "cash", "bonus", "b2b", "b2b2c", "hamyar"}


def get_user_ids(client: pymongo.MongoClient, usernames: List[str]) -> Dict:
    """
    مرحله 1: پیدا کردن userId برای هر username
    """
    db = client["fadax"]
    user_col = db["users"]

    results = {
        "found": {},  # username هایی که پیدا شدند
        "not_found": []  # username هایی که پیدا نشدند
    }

    logger.info("=" * 60)
    logger.info("🔍 مرحله 1: شروع جستجوی کاربران...")
    logger.info("=" * 60)

    for username in usernames:
        user = user_col.find_one({"username": username}, {"id": 1, "username": 1})

        if user:
            user_id = user.get("id")
            results["found"][username] = user_id
            logger.info(f"✅ {username} -> userId: {user_id}")
        else:
            results["not_found"].append(username)
            logger.warning(f"❌ {username} -> کاربر یافت نشد!")

    logger.info("=" * 60)
    logger.info(f"📊 نتیجه مرحله 1: {len(results['found'])} کاربر پیدا شد")
    logger.info("=" * 60)

    return results


def check_user_debts(client: pymongo.MongoClient, user_data: Dict) -> Dict:
    """
    مرحله 2: چک کردن بدهی کاربران از کالکشن installments
    """
    db = client["fadax"]
    installments_col = db["installments"]

    results = {
        "no_debt": {},  # کاربران بدون بدهی: {username: userId}
        "has_debt": {}  # کاربران با بدهی: {username: {userId: x, debt_count: y}}
    }

    logger.info("\n" + "=" * 60)
    logger.info("💰 مرحله 2: شروع چک کردن بدهی...")
    logger.info("=" * 60)

    for username, user_id in user_data["found"].items():
        # جستجو در installments
        debt_query = {
            "userId": user_id,
            "status": {"$in": ["unpaid", "init"]}
        }

        debt_count = installments_col.count_documents(debt_query)

        if debt_count == 0:
            # بدهی نداره
            results["no_debt"][username] = user_id
            logger.info(f"✅ {username} (userId: {user_id}) -> بدون بدهی")
        else:
            # بدهی داره
            results["has_debt"][username] = {
                "userId": user_id,
                "debt_count": debt_count
            }
            logger.warning(f"🔴 {username} (userId: {user_id}) -> {debt_count} قسط پرداخت نشده دارد!")

    # خلاصه نتایج
    logger.info("=" * 60)
    logger.info(f"📊 نتیجه مرحله 2:")
    logger.info(f"   ✅ کاربران بدون بدهی: {len(results['no_debt'])}")
    logger.info(f"   🔴 کاربران با بدهی: {len(results['has_debt'])}")
    logger.info("=" * 60)

    return results


def collect_wallet_ids(client: pymongo.MongoClient, user_data: Dict) -> Dict:
    """
    مرحله 3: پیدا کردن wallet های قابل صفر کردن
    """
    db = client["fadax"]
    wallet_col = db["wallets"]
    credit_request_col = db["creditRequest"]

    results = {
        "wallet_ids": set(),  # ID های wallet هایی که باید صفر شوند
        "wallet_details": [],  # جزئیات wallet ها برای لاگ
        "skipped_wallets": []  # wallet هایی که skip شدند
    }

    logger.info("\n" + "=" * 60)
    logger.info("💼 مرحله 3: شروع پیدا کردن wallet ها...")
    logger.info("=" * 60)

    for username, user_id in user_data.items():
        logger.info(f"\n🔍 بررسی wallet های {username} (userId: {user_id})...")

        # پیدا کردن تمام wallet های این کاربر
        wallets = wallet_col.find({"userId": user_id})
        user_wallet_count = 0

        for wallet in wallets:
            user_wallet_count += 1
            wallet_id = wallet["_id"]
            w_type = wallet.get("type")
            balance = wallet.get("balance", 0)

            # بررسی نوع wallet
            if w_type in OTHER_WALLET_TYPES:
                # این wallet ها را صفر نمی‌کنیم
                results["skipped_wallets"].append({
                    "wallet_id": str(wallet_id),
                    "type": w_type,
                    "reason": "نوع wallet در لیست OTHER_WALLET_TYPES است"
                })
                logger.info(f"   ⏭️  Wallet {wallet_id} (type: {w_type}) -> Skip شد (OTHER_WALLET_TYPES)")
                continue

            if w_type in MAIN_WALLET_TYPES:
                # این wallet ها را حتماً صفر می‌کنیم
                results["wallet_ids"].add(wallet_id)
                results["wallet_details"].append({
                    "wallet_id": str(wallet_id),
                    "username": username,
                    "type": w_type,
                    "balance": balance
                })
                logger.info(f"   ✅ Wallet {wallet_id} (type: {w_type}, balance: {balance}) -> اضافه شد")
                continue

            if w_type == "guarantee":
                # منطق خاص برای guarantee
                max_credit = wallet.get("maxCredit", 0)

                if balance < max_credit:
                    logger.info(
                        f"   🔎 Wallet {wallet_id} (guarantee, balance: {balance}, maxCredit: {max_credit}) -> بررسی creditRequest...")

                    # پیدا کردن creditRequest های مرتبط
                    credit_requests = credit_request_col.find({
                        "guaranteeOption.refrenceWalletId": wallet_id
                    })

                    for cr in credit_requests:
                        # پیدا کردن contract wallet های مرتبط
                        contract_wallets = wallet_col.find({
                            "contract.creditRequestId": cr["_id"]
                        }, {"_id": 1, "type": 1, "balance": 1})

                        for cw in contract_wallets:
                            results["wallet_ids"].add(cw["_id"])
                            results["wallet_details"].append({
                                "wallet_id": str(cw["_id"]),
                                "username": username,
                                "type": cw.get("type"),
                                "balance": cw.get("balance", 0),
                                "related_to": f"guarantee wallet {wallet_id}"
                            })
                            logger.info(f"      ✅ Contract Wallet {cw['_id']} (مرتبط با guarantee) -> اضافه شد")
                else:
                    results["skipped_wallets"].append({
                        "wallet_id": str(wallet_id),
                        "type": w_type,
                        "reason": f"balance ({balance}) >= maxCredit ({max_credit})"
                    })
                    logger.info(f"   ⏭️  Wallet {wallet_id} (guarantee) -> Skip شد (balance >= maxCredit)")

        logger.info(f"✅ کاربر {username}: {user_wallet_count} wallet بررسی شد")

    # خلاصه نتایج
    logger.info("=" * 60)
    logger.info(f"📊 نتیجه مرحله 3:")
    logger.info(f"   ✅ Wallet های قابل صفر کردن: {len(results['wallet_ids'])}")
    logger.info(f"   ⏭️  Wallet های Skip شده: {len(results['skipped_wallets'])}")
    logger.info("=" * 60)

    return results


def reset_wallet_balances(client: pymongo.MongoClient, wallet_ids: Set, wallet_details: List[Dict]) -> Dict:
    """
    مرحله 4: صفر کردن بالانس wallet ها از طریق API
    """
    if not wallet_ids:
        logger.info("هیچ wallet ای برای صفر کردن یافت نشد.")
        return {"success": [], "failed": []}

    db = client["fadax"]
    wallet_col = db["wallets"]

    logger.info("\n" + "=" * 60)
    logger.info("🔄 مرحله 4: شروع صفر کردن بالانس wallet ها...")
    logger.info("=" * 60)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "accept": "*/*"
    }

    results = {
        "success": [],
        "failed": []
    }

    session = requests.Session()
    session.headers.update(headers)

    # ایجاد یک دیکشنری برای دسترسی سریع به جزئیات wallet
    wallet_info_map = {detail["wallet_id"]: detail for detail in wallet_details}

    total = len(wallet_ids)
    current = 0

    for wallet_id in wallet_ids:
        current += 1
        wallet_info = wallet_info_map.get(str(wallet_id), {})
        username = wallet_info.get("username", "نامشخص")
        w_type = wallet_info.get("type", "نامشخص")
        old_balance = wallet_info.get("balance", 0)

        logger.info(f"\n[{current}/{total}] در حال صفر کردن Wallet {wallet_id}...")
        logger.info(f"   کاربر: {username}")
        logger.info(f"   نوع: {w_type}")
        logger.info(f"   بالانس فعلی: {old_balance}")

        payload = {
            "walletId": str(wallet_id),
            "newAmount": 0
        }

        try:
            response = session.post(
                f"{BASE_URL}/user/wallet/change-balance",
                json=payload,
                timeout=10
            )
            response.raise_for_status()

            wallet_col.update_one(
                {"_id": wallet_id},
                {"$set": {"isActive": False}}
            )

            logger.info("   🔒 Wallet غیرفعال شد (isActive: false)")

            result_data = {
                "wallet_id": str(wallet_id),
                "username": username,
                "type": w_type,
                "old_balance": old_balance,
                "new_balance": 0,
                "response": response.json()
            }

            results["success"].append(result_data)
            logger.info(f"   ✅ موفق - بالانس از {old_balance} به 0 تغییر کرد")

        except requests.RequestException as e:
            error_msg = str(e)

            result_data = {
                "wallet_id": str(wallet_id),
                "username": username,
                "type": w_type,
                "old_balance": old_balance,
                "error": error_msg
            }

            results["failed"].append(result_data)
            logger.error(f"   ❌ خطا: {error_msg}")

    # خلاصه نتایج
    logger.info("\n" + "=" * 60)
    logger.info(f"📊 نتیجه مرحله 4:")
    logger.info(f"   ✅ موفق: {len(results['success'])} wallet")
    logger.info(f"   ❌ ناموفق: {len(results['failed'])} wallet")
    logger.info("=" * 60)

    return results


def cancel_credit_requests(client: pymongo.MongoClient, usernames: List[str], log_message: str) -> Dict:
    """
    مرحله 5: لغو کردن creditRequest های کاربران
    """
    db = client["fadax"]
    credit_request_col = db["creditRequest"]

    logger.info("\n" + "=" * 60)
    logger.info("🚫 مرحله 5: شروع لغو creditRequest ها...")
    logger.info("=" * 60)

    results = {
        "canceled": {},  # {username: count}
        "not_found": []  # usernames without creditRequest
    }

    for username in usernames:
        logger.info(f"\n🔍 بررسی creditRequest های {username}...")

        result = credit_request_col.update_many(
            {"phoneNumber": username},
            {
                "$set": {
                    "state": "canceled",
                    "canceledAt": datetime.now(timezone.utc),
                    "log": log_message
                }
            }
        )

        if result.modified_count > 0:
            results["canceled"][username] = result.modified_count
            logger.info(f"   ✅ {result.modified_count} creditRequest لغو شد")
        else:
            results["not_found"].append(username)
            logger.info(f"   ℹ️  هیچ creditRequest فعالی یافت نشد")

    # خلاصه نتایج
    logger.info("\n" + "=" * 60)
    logger.info(f"📊 نتیجه مرحله 5:")
    total_canceled = sum(results["canceled"].values())
    logger.info(f"   ✅ تعداد کل creditRequest های لغو شده: {total_canceled}")
    logger.info(f"   ℹ️  کاربران بدون creditRequest: {len(results['not_found'])}")
    logger.info("=" * 60)

    return results


def main():
    # 📝 لیست username ها
    usernames = [
        '09374712278',
        '09375087045',
        '09128787340',
        '09052195900'
    ]

    # 📝 پیام لاگ برای لغو creditRequest ها
    log_message = "لغو به دلیل تسویه حساب / عملیات دستی ادمین"

    client = pymongo.MongoClient(MONGO_URI)

    try:
        # مرحله 1: پیدا کردن userId ها
        user_data = get_user_ids(client, usernames)

        if not user_data["found"]:
            logger.error("❌ هیچ کاربری یافت نشد. عملیات متوقف شد.")
            return

        # مرحله 2: چک کردن بدهی
        debt_results = check_user_debts(client, user_data)

        # اگر هیچ کاربر بدون بدهی نداریم، متوقف می‌شویم
        if not debt_results["no_debt"]:
            logger.error("\n❌ همه کاربران دارای بدهی هستند. عملیات متوقف شد.")
            return

        # نمایش گزارش بدهی
        logger.info("\n" + "=" * 60)
        logger.info("📋 گزارش بدهی:")
        logger.info("=" * 60)

        if debt_results["no_debt"]:
            logger.info("\n✅ کاربران بدون بدهی (ادامه عملیات):")
            for username, user_id in debt_results["no_debt"].items():
                logger.info(f"   • {username} (userId: {user_id})")

        if debt_results["has_debt"]:
            logger.warning("\n🔴 کاربران با بدهی (هیچ کاری انجام نمی‌شود):")
            for username, info in debt_results["has_debt"].items():
                logger.warning(f"   • {username} (userId: {info['userId']}) -> {info['debt_count']} قسط پرداخت نشده")

        logger.info("=" * 60)

        # مرحله 3: پیدا کردن wallet ها
        wallet_results = collect_wallet_ids(client, debt_results["no_debt"])

        if not wallet_results["wallet_ids"]:
            logger.warning("\n⚠️  هیچ wallet ای برای صفر کردن یافت نشد.")
            # حتی اگر wallet نباشه، ادامه بده برای لغو creditRequest ها
        else:
            # نمایش جزئیات wallet های پیدا شده
            logger.info("\n📋 لیست wallet های قابل صفر کردن:")
            for detail in wallet_results["wallet_details"]:
                related = f" (مرتبط با {detail['related_to']})" if "related_to" in detail else ""
                logger.info(f"   • Wallet ID: {detail['wallet_id']}")
                logger.info(f"     - کاربر: {detail['username']}")
                logger.info(f"     - نوع: {detail['type']}")
                logger.info(f"     - بالانس فعلی: {detail['balance']}{related}")

            # مرحله 4: صفر کردن بالانس wallet ها
            reset_results = reset_wallet_balances(
                client,
                wallet_results["wallet_ids"],
                wallet_results["wallet_details"]
            )

        # مرحله 5: لغو creditRequest ها (فقط برای کاربران بدون بدهی)
        usernames_without_debt = list(debt_results["no_debt"].keys())
        cancel_results = cancel_credit_requests(client, usernames_without_debt, log_message)

        # گزارش نهایی
        logger.info("\n" + "=" * 60)
        logger.info("🏁 گزارش نهایی:")
        logger.info("=" * 60)
        logger.info(f"📊 کاربران:")
        logger.info(f"   • بررسی شده: {len(usernames)}")
        logger.info(f"   • بدون بدهی: {len(debt_results['no_debt'])}")
        logger.info(f"   • با بدهی: {len(debt_results['has_debt'])}")

        if wallet_results["wallet_ids"]:
            logger.info(f"\n💼 Wallet ها:")
            logger.info(f"   • شناسایی شده: {len(wallet_results['wallet_ids'])}")
            logger.info(f"   • صفر شده: {len(reset_results['success'])}")
            logger.info(f"   • ناموفق: {len(reset_results['failed'])}")

        logger.info(f"\n🚫 CreditRequest ها:")
        total_canceled = sum(cancel_results["canceled"].values())
        logger.info(f"   • لغو شده: {total_canceled}")
        logger.info(f"   • کاربران بدون creditRequest: {len(cancel_results['not_found'])}")

        # نمایش wallet های ناموفق اگر وجود داشته باشند
        if wallet_results["wallet_ids"] and reset_results["failed"]:
            logger.error("\n❌ لیست wallet های ناموفق:")
            for failed in reset_results["failed"]:
                logger.error(f"   • Wallet {failed['wallet_id']} ({failed['username']})")
                logger.error(f"     خطا: {failed['error']}")

        logger.info("=" * 60)
        logger.info("✅ عملیات با موفقیت تمام شد!")
        logger.info("=" * 60)

    except Exception as e:
        logger.exception(f"❌ خطا: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
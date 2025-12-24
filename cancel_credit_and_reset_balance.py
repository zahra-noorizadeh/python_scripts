import os
import logging
from datetime import datetime, timezone
from typing import List, Set, Dict

import pymongo
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_KEY")
BASE_URL = os.getenv("BASE_URL")
MONGO_URI = os.getenv("MONGO_URI")

if not all([API_KEY, BASE_URL, MONGO_URI]):
    raise ValueError("یکی از متغیرهای محیطی API_KEY, BASE_URL یا MONGO_URI تنظیم نشده است.")

MAIN_WALLET_TYPES = {"b2c", "mohlat"}
OTHER_WALLET_TYPES = {"travel", "tajan", "cash", "bonus", "b2b", "b2b2c", "hamyar"}


def get_user_ids(client: pymongo.MongoClient, phone_numbers: List[str]) -> Dict:
    db = client["fadax"]
    user_col = db["users"]
    user_ids = []
    phone_to_user = {}

    for phone in phone_numbers:
        user = user_col.find_one({"phoneNumber": phone}, {"id": 1})
        if not user:
            logger.warning("⚠️ کاربر با شماره %s یافت نشد.", phone)
            continue
        user_ids.append(user["id"])
        phone_to_user[phone] = user["id"]

    return {"user_ids": user_ids, "phone_to_user": phone_to_user}


def check_debt_users(client: pymongo.MongoClient, user_data: dict) -> Dict:

    user_ids = user_data["user_ids"]
    phone_to_user = user_data["phone_to_user"]

    if not user_ids:
        return {"paid_phones": [], "debtor_phones": []}

    db = client["fadax"]
    installment_col = db["installments"]

    unpaid_user_ids = list(
        installment_col.find(
            {
                "userId": {"$in": user_ids},
                "status": {"$in": ["init", "unpaid"]}
            }
        )
    )

    paid_phones = []
    debtor_phones = []

    for phone, user_id in phone_to_user.items():
        if user_id in unpaid_user_ids:
            debtor_phones.append(phone)
            logger.warning("🔴 کاربر با شماره %s (userId: %s) دارای بدهی است - هیچ عملیاتی انجام نمی‌شود!", phone,
                           user_id)
        else:
            paid_phones.append(phone)
            logger.info("✅ کاربر با شماره %s (userId: %s) بدون بدهی است.", phone, user_id)

    return {"paid_phones": paid_phones, "debtor_phones": debtor_phones}


def collect_wallet_ids_to_reset(client: pymongo.MongoClient, phone_numbers: List[str]) -> Set[str]:
    db = client["fadax"]
    wallet_col = db["wallets"]
    credit_request_col = db["creditRequest"]

    wallet_ids = set()

    for phone in phone_numbers:
        wallets = wallet_col.find({"phoneNumber": phone})

        for wallet in wallets:
            w_type = wallet.get("type")

            if w_type in OTHER_WALLET_TYPES:
                continue

            if w_type in MAIN_WALLET_TYPES:
                wallet_ids.add(wallet["_id"])
                continue

            if w_type == "guarantee":
                balance = wallet.get("balance", 0)
                max_credit = wallet.get("maxCredit", 0)

                if balance < max_credit:
                    credit_requests = credit_request_col.find({
                        "guaranteeOption.refrenceWalletId": wallet["_id"]
                    })

                    for cr in credit_requests:
                        contract_wallets = wallet_col.find({
                            "contract.creditRequestId": cr["_id"]
                        }, {"_id": 1})

                        for cw in contract_wallets:
                            wallet_ids.add(cw["_id"])

    return wallet_ids


def reset_wallet_balances(wallet_ids: Set[str]) -> List[dict]:
    if not wallet_ids:
        logger.info("هیچ کیف پولی برای صفر کردن یافت نشد.")
        return []

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "accept": "*/*"
    }

    results = []
    session = requests.Session()
    session.headers.update(headers)

    for wallet_id in wallet_ids:
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
            results.append({"wallet_id": str(wallet_id), "success": True, "data": response.json()})
            logger.info("✅ بالانس کیف %s با موفقیت صفر شد.", wallet_id)
        except requests.RequestException as e:
            error_msg = str(e)
            results.append({"wallet_id": str(wallet_id), "success": False, "error": error_msg})
            logger.error("❌ خطا در صفر کردن بالانس کیف %s: %s", wallet_id, error_msg)

    return results


def cancel_credit_requests(client: pymongo.MongoClient, phone_numbers: List[str], log_message: str):
    db = client["fadax"]
    credit_request_col = db["creditRequest"]

    for phone in phone_numbers:
        result = credit_request_col.update_many(
            {"phoneNumber": phone},
            {
                "$set": {
                    "state": "canceled",
                    "canceledAt": datetime.now(timezone.utc),
                    "log": log_message
                }
            }
        )
        if result.modified_count > 0:
            logger.info("✅ %d درخواست اعتباری برای شماره %s لغو شد.", result.modified_count, phone)
        else:
            logger.info("ℹ️ هیچ درخواست اعتباری برای شماره %s یافت نشد.", phone)


def main():
    phone_numbers = [
        "09331672753"
    ]
    log_message = "لغو به دلیل تسویه حساب / عملیات دستی ادمین"

    client = pymongo.MongoClient(MONGO_URI)

    try:
        # گام 1: دریافت اطلاعات کاربران
        user_data = get_user_ids(client, phone_numbers)
        if not user_data["user_ids"]:
            logger.error("❌ هیچ کاربری یافت نشد. عملیات متوقف شد.")
            return

        # گام 2: چک کردن بدهی کاربران (اولین و مهم‌ترین گام)
        debt_result = check_debt_users(client, user_data)
        paid_phones = debt_result["paid_phones"]
        debtor_phones = debt_result["debtor_phones"]

        # نمایش گزارش بدهکاران
        if debtor_phones:
            logger.warning("=" * 60)
            logger.warning("🔴 کاربران دارای بدهی (هیچ عملیاتی انجام نمی‌شود):")
            for phone in debtor_phones:
                logger.warning(f"   - {phone}")
            logger.warning("=" * 60)

        # اگر هیچ کاربر بدون بدهی نداریم، متوقف می‌شویم
        if not paid_phones:
            logger.error("❌ همه کاربران دارای بدهی هستند. عملیات متوقف شد.")
            logger.error("❌ لطفاً ابتدا بدهی کاربران زیر را تسویه کنید:")
            for phone in debtor_phones:
                logger.error(f"   - {phone}")
            return

        # گام 3: ادامه عملیات فقط برای کاربران بدون بدهی
        logger.info("=" * 60)
        logger.info("✅ شروع عملیات برای کاربران بدون بدهی:")
        for phone in paid_phones:
            logger.info(f"   - {phone}")
        logger.info("=" * 60)

        wallet_ids_to_reset = collect_wallet_ids_to_reset(client, paid_phones)
        reset_results = reset_wallet_balances(wallet_ids_to_reset)
        cancel_credit_requests(client, paid_phones, log_message)

        # گزارش نهایی
        success_count = sum(1 for r in reset_results if r.get("success"))
        logger.info("=" * 60)
        logger.info("✅ عملیات تمام شد:")
        logger.info(f"   - کاربران پردازش شده: {len(paid_phones)}")
        logger.info(f"   - کاربران با بدهی (رد شده): {len(debtor_phones)}")
        logger.info(f"   - کیف پول‌های صفر شده: {success_count}")
        logger.info("=" * 60)

    except Exception as e:
        logger.exception("❌ خطای غیرمنتظره در اجرای اسکریپت: %s", e)
    finally:
        client.close()


if __name__ == "__main__":
    main()
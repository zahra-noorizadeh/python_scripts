import os
import logging
from datetime import datetime, timezone
from typing import List, Set

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


def get_user_ids(client: pymongo.MongoClient, phone_numbers: List[str]) -> List[str]:
    db = client["fadax"]
    user_col = db["users"]
    user_ids = []

    for phone in phone_numbers:
        user = user_col.find_one({"phoneNumber": phone}, {"id": 1})
        if not user:
            logger.warning("کاربر با شماره %s یافت نشد.", phone)
            continue
        user_ids.append(user["id"])

    return user_ids


def check_debt_users(client: pymongo.MongoClient, user_ids: List[str]) -> List[str]:
    if not user_ids:
        return []

    db = client["fadax"]
    user_col = db["users"]

    unpaid_users = user_col.distinct(
        "userId",
        {"userId": {"$in": user_ids}, "status": {"$in": ["init", "unpaid"]}}
    )

    paid_users = [uid for uid in user_ids if uid not in unpaid_users]
    for uid in unpaid_users:
        logger.info("کاربر با شماره کاربری %s دارای بدهی می‌باشد.", uid)

    return paid_users


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
            logger.info("بالانس کیف %s با موفقیت صفر شد.", wallet_id)
        except requests.RequestException as e:
            error_msg = str(e)
            results.append({"wallet_id": str(wallet_id), "success": False, "error": error_msg})
            logger.error("خطا در صفر کردن بالانس کیف %s: %s", wallet_id, error_msg)

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
            logger.info("%d درخواست اعتباری برای شماره %s لغو شد.", result.modified_count, phone)
        else:
            logger.info("هیچ درخواست اعتباری برای شماره %s یافت نشد.", phone)


def main():
    phone_numbers = ["09056975170", "09387029284"]
    log_message = "لغو به دلیل تسویه حساب / عملیات دستی ادمین"  # می‌تونی این رو پارامتر کنی

    client = pymongo.MongoClient(MONGO_URI)

    try:
        user_ids = get_user_ids(client, phone_numbers)
        if not user_ids:
            logger.error("هیچ کاربری یافت نشد. عملیات متوقف شد.")
            return

        paid_user_ids = check_debt_users(client, user_ids)
        if not paid_user_ids:
            logger.warning("هیچ کاربری بدون بدهی نیست. عملیات ادامه نمی‌یابد.")
            return

        wallet_ids_to_reset = collect_wallet_ids_to_reset(client, phone_numbers)

        reset_results = reset_wallet_balances(wallet_ids_to_reset)

        cancel_credit_requests(client, phone_numbers, log_message)

        success_count = sum(1 for r in reset_results if r.get("success"))
        logger.info("عملیات تمام شد. %d کیف پول با موفقیت صفر شد.", success_count)

    except Exception as e:
        logger.exception("خطای غیرمنتظره در اجرای اسکریپت: %s", e)
    finally:
        client.close()


if __name__ == "__main__":
    main()
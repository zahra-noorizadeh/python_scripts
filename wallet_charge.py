from pymongo import MongoClient
from dotenv import load_dotenv
import requests
import os


load_dotenv()
MONGO_URI: str = os.getenv("MONGO_URI")
TOKEN = os.getenv("TOKEN")
API_URL = os.getenv("API_URL")


def get_wallets(clientId: int):
    client = MongoClient(MONGO_URI)
    db = client["fadax_dev"]
    wallets_col = db["wallets"]
    installments_col = db["installments"]
    events_col = db["events"]

    try:
        print("Server info:", client.server_info())
        print("Databases:", client.list_database_names())
    except Exception as e:
        print("Connection failed:", e)


    wallets = list(wallets_col.find({"contract.clientId": clientId}))
    results = []

    for wallet in wallets:
        if wallet["balance"] == wallet["maxCredit"]:
            continue

        wallet_id = str(wallet["_id"])
        wallet_balance = wallet["balance"]
        count = 0

        installments = list(installments_col.find({"walletId": wallet_id, "status": "paid", "type": "installment"}))
        events = list(events_col.find({"aggregateRootId": wallet_id}))

        for installment in installments:
            matched = False
            for event in events:
                time_diff = abs((installment["updatedAt"] - event["createdAt"]).total_seconds())

                if time_diff < 2 and \
                        event.get("payload", {}).get("value") == installment["amountWithoutCost"] and \
                        event.get("eventName") == "balance-increased-event":
                    matched = True
                    break

            if not matched:
                wallet_balance += installment["amountWithoutCost"]
                count += 1

        results.append({
            "walletId": wallet_id,
            "newAmount": wallet_balance,
            "numberOfAddedInstallments": count
        })

    return results


def update_wallet_amounts(clientId: int):
    results = get_wallets(clientId)

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }

    print("RESULTS:", results)
    for item in results:
        payload = {
            "walletId": item["walletId"],
            "newAmount": item["newAmount"]
        }

        response = requests.post(API_URL, headers=headers, json=payload)

        if response.status_code in (200, 201):
            print(f"✅ Updated wallet {item['walletId']} successfully.")
        else:
            print(f"❌ Failed for {item['walletId']}: {response.status_code} - {response.text}")


update_wallet_amounts(63)

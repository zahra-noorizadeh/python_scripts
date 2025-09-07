import requests
import csv
import pandas as pd
from bson import ObjectId
from datetime import datetime, timezone
from dotenv import load_dotenv
import os


load_dotenv()
MONGO_URI: str = os.getenv("MONGO_URI")
TOKEN = os.getenv("TOKEN")


def get_orders(supplier_id: str, created_at: str):
    client = MONGO_URI
    db = client["fadax"]
    collection = db["orders"]

    try:
        print("Server info:", client.server_info())
        print("Databases:", client.list_database_names())
    except Exception as e:
        print("Connection failed:", e)


    created_at_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

    pipeline = [
        {
            "$match": {
                "supplierId": ObjectId(supplier_id),
                "isFinalized": False,
                "status": "complete",
                "createdAt": {"$gte": created_at_dt},
            }
        },
        {
            "$project": {
                "status": 1,
                "paymentToken": 1,
                "isFinalized": 1,
                "createdAt": 1,
                "supplierId": 1,
                "invoiceId": 1,
            }
        },
    {
        "$limit": 100
    }
    ]

    result = collection.aggregate(pipeline)

    orders = []
    for data in result:
        if "createdAt" in data:
            data["createdAt"] = data["createdAt"].astimezone(timezone.utc).isoformat()
        orders.append(data)

    return orders


def finalize_order(object_id):
    baseurl = "https://api.fadax.ir/order"
    token: str = TOKEN

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    url = f"{baseurl}/{object_id}/finalize"
    response = requests.put(url, headers=headers)

    try:
        data = response.json()
    except Exception:
        data = None

    return response.status_code, response.text, data


results = []
orders = get_orders("63bc0d403c4d6f1db1ef0008", "2025-07-23T00:00:00.794Z")

for order in orders:
    object_id = str(order["_id"])
    invoice_id = order.get("invoiceId")
    status, text, json_data = finalize_order(object_id)
    success = 200 <= status < 300

    results.append({
        "order_id": object_id,
        "invoiceId": invoice_id,
        "success": success,
        "status_code": status,
        "response": text
    })

    print(f"Order {object_id} (Invoice {invoice_id}): {'Success' if success else 'Failed'} ({status})")



with open("results.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["order_id", "invoiceId", "success", "status_code", "response"])
    writer.writeheader()
    writer.writerows(results)



df = pd.DataFrame(results)
df.to_csv("results_pandas.csv", index=False, encoding="utf-8")

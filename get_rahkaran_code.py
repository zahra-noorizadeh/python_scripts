from pymongo import MongoClient
from dotenv import load_dotenv
import os
from read_excel_column import read_excel_column
import pandas as pd

file_path = "C:/Users/CRM/Downloads/تفصیلی ها - جامع - از روی فایل ارسالی خانم نوری زاده - 14040817.xlsx"
column_name = 'LN'
output_file = 'C:/Users/CRM/Desktop/rahkaran_codes_output2.xlsx'

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    raise ValueError("MONGO_URI not found in .env")

print(f"اتصال به: {MONGO_URI}")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client["fadax"]
creditRequest_col = db["creditRequest"]

ln_list = read_excel_column(file_path, column_name)
print(f"تعداد LNهای خوانده شده: {len(ln_list)}")

results = []

for ln in ln_list:
    ln = str(ln).strip()
    pipeline = [
        {"$match": {"creditLineMeta.loanApplicationRef": ln}},
        {
            "$lookup": {
                "from": "users",
                "localField": "phoneNumber",
                "foreignField": "phoneNumber",
                "as": "user_info"
            }
        },
        {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "_id": 0,
                "loanApplicationRef": "$creditLineMeta.loanApplicationRef",
                "rahkaranCode": "$user_info.rahkaranCode"
            }
        }
    ]

    result = list(creditRequest_col.aggregate(pipeline))

    if result:
        rahkaran_code = result[0].get('rahkaranCode')
        print(f"{ln} -> {rahkaran_code}")
        results.append({"LN": ln, "rahkaranCode": rahkaran_code})
    else:
        print(f"{ln} -> پیدا نشد")
        results.append({"LN": ln, "rahkaranCode": None})

df = pd.DataFrame(results)
df.to_excel(output_file, index=False)
print(f"\nنتایج در فایل ذخیره شد:\n{output_file}")

client.close()
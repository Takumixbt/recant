"""Read this account's Bedrock quotas and retry a single embed call."""
import json, os, time
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
REGION = os.environ["AWS_REGION"]

sq = boto3.client("service-quotas", region_name=REGION)
print("--- Bedrock quotas mentioning Claude/Titan tokens ---")
try:
    paginator = sq.get_paginator("list_service_quotas")
    rows = []
    for page in paginator.paginate(ServiceCode="bedrock"):
        for q in page["Quotas"]:
            n = q["QuotaName"]
            if any(k in n for k in ("Haiku 4.5", "Titan Text Embeddings V2", "Sonnet 4.6")):
                rows.append((q["Value"], n))
    for v, n in sorted(rows):
        print(f"  {v:>12,.0f}  {n[:88]}")
    if not rows:
        print("  (no matching quota rows returned)")
except ClientError as e:
    print("  service-quotas read failed:", e.response["Error"]["Code"])

print("\n--- embed retry (3 attempts, backing off) ---")
rt = boto3.client("bedrock-runtime", region_name=REGION)
body = json.dumps({"inputText": "allergic to penicillin", "dimensions": 1024, "normalize": True})
for i in range(1, 4):
    try:
        r = rt.invoke_model(modelId=os.environ["BEDROCK_EMBED_MODEL"], body=body,
                            accept="application/json", contentType="application/json")
        emb = json.loads(r["body"].read())["embedding"]
        print(f"  attempt {i}: PASS  dims={len(emb)}")
        break
    except ClientError as e:
        print(f"  attempt {i}: {e.response['Error']['Code']}: {e.response['Error']['Message'][:70]}")
        time.sleep(i * 5)

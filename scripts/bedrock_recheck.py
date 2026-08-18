import json, os, boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
load_dotenv()
rt = boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"])
print("--- Titan embeddings ---")
try:
    r = rt.invoke_model(modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText":"customer is allergic to penicillin","dimensions":1024,"normalize":True}),
        accept="application/json", contentType="application/json")
    e = json.loads(r["body"].read())["embedding"]
    print(f"  PASS  dims={len(e)}  head={[round(x,4) for x in e[:3]]}")
except ClientError as ex:
    print("  FAIL ", ex.response["Error"]["Code"], ex.response["Error"]["Message"][:110])
print("--- Claude chat ---")
try:
    r = rt.converse(modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        messages=[{"role":"user","content":[{"text":"Reply with exactly: OK"}]}],
        inferenceConfig={"maxTokens":10,"temperature":0})
    print("  PASS ", r["output"]["message"]["content"][0]["text"].strip())
except ClientError as ex:
    print("  FAIL ", ex.response["Error"]["Code"], ex.response["Error"]["Message"][:110])

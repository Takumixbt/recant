"""
Verify the AWS half of the stack: identity, embeddings, and a chat model.

Claude on Bedrock increasingly requires a cross-region inference profile
(the "us." prefix) rather than a bare model id, so we probe candidates and
report which one actually works.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
REGION = os.environ.get("AWS_REGION", "us-east-1")

print("=" * 78)
print("  RECANT :: AWS Bedrock check")
print("=" * 78)

# --- identity -------------------------------------------------------------
try:
    ident = boto3.client("sts", region_name=REGION).get_caller_identity()
    print(f"  PASS  identity   account={ident['Account']} arn={ident['Arn']}")
except ClientError as e:
    print(f"  FAIL  identity   {e}")
    raise SystemExit(1)

bedrock = boto3.client("bedrock", region_name=REGION)
runtime = boto3.client("bedrock-runtime", region_name=REGION)

# --- what's actually offered ----------------------------------------------
try:
    models = bedrock.list_foundation_models()["modelSummaries"]
    ids = [m["modelId"] for m in models]
    print(f"  PASS  catalog    {len(ids)} models visible in {REGION}")
    for pat in ("titan-embed-text-v2", "claude-3-5-haiku", "claude-3-5-sonnet"):
        hits = [i for i in ids if pat in i]
        print(f"        {pat:24} -> {hits or 'none'}")
except ClientError as e:
    print(f"  FAIL  catalog    {e.response['Error']['Code']}: {e.response['Error']['Message'][:90]}")

# --- embeddings (Amazon model, should need no form) -----------------------
EMBED = os.environ.get("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
try:
    r = runtime.invoke_model(
        modelId=EMBED,
        body=json.dumps({"inputText": "allergic to penicillin",
                         "dimensions": 1024, "normalize": True}),
        accept="application/json", contentType="application/json",
    )
    emb = json.loads(r["body"].read())["embedding"]
    print(f"  PASS  embed      {EMBED}  dims={len(emb)}  head={[round(x,4) for x in emb[:3]]}")
except ClientError as e:
    print(f"  FAIL  embed      {e.response['Error']['Code']}: {e.response['Error']['Message'][:110]}")

# --- chat model (Anthropic, needed the use-case form) ---------------------
CANDIDATES = [
    os.environ.get("BEDROCK_CHAT_MODEL", ""),
    # Real IDs read from this account's catalog. Every current Claude on Bedrock
    # is INFERENCE_PROFILE-only, so the "us." prefix is required, and Haiku 4.5
    # keeps a date suffix while Sonnet 5 / Opus 5 do not.
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-5",
    "us.anthropic.claude-opus-5",
    "us.anthropic.claude-sonnet-4-6",
]
seen, working = set(), None
for mid in [c for c in CANDIDATES if c and not (c in seen or seen.add(c))]:
    try:
        r = runtime.converse(
            modelId=mid,
            messages=[{"role": "user", "content": [{"text": "Reply with exactly: OK"}]}],
            inferenceConfig={"maxTokens": 10, "temperature": 0},
        )
        txt = r["output"]["message"]["content"][0]["text"].strip()
        print(f"  PASS  chat       {mid}  -> {txt!r}")
        working = mid
        break
    except ClientError as e:
        code = e.response["Error"]["Code"]
        print(f"  fail  chat       {mid}  [{code}] {e.response['Error']['Message'][:80]}")

print("=" * 78)
if working:
    print(f"  Use BEDROCK_CHAT_MODEL={working}")
else:
    print("  NO CHAT MODEL AVAILABLE -- use-case form may still be settling.")
print("=" * 78)

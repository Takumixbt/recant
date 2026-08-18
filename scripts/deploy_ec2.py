"""
Deploy the console to a public URL on EC2.

Chosen over Lambda/App Runner deliberately: the app carries an ONNX embedding
model, so a Lambda container image is ~2GB and the ECR push dominates. EC2 with
a user-data bootstrap needs no local Docker build, no image push, and no console
clicks -- the repo is public, so the instance clones it on boot.

Creates: security group (port 80 + 22), key-less instance, elastic-IP-free
public DNS. Prints the demo URL when the app answers.

Usage: python scripts/deploy_ec2.py [--teardown]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

REGION = os.environ.get("AWS_REGION", "us-east-1")
NAME = "recant-demo"
REPO = "https://github.com/Takumixbt/recant.git"
PORT = 80

USER_DATA = """#!/bin/bash
set -xe
dnf install -y python3.11 python3.11-pip git >/tmp/dnf.log 2>&1
cd /opt
git clone {repo} recant
cd recant
python3.11 -m pip install --quiet -r requirements.txt
# CockroachDB Cloud chains to ISRG Root X2, which the OS trust store does not
# always resolve. certifi carries it, so pin that explicitly rather than relying
# on sslrootcert=system.
CACERT=$(python3.11 -c "import certifi; print(certifi.where())")
cat > .env <<ENVEOF
DATABASE_URL={db}&sslrootcert=$CACERT
EMBED_PROVIDER=local
MODEL_PROVIDER=rule
AWS_REGION={region}
RECANT_POOL_MAX=8
ENVEOF
# warm the embedding model before serving so the first request is not a 90s wait
python3.11 -c "import sys; sys.path.insert(0,'.'); from recant.embed import get_embedder; get_embedder().embed('warm')" || true
nohup python3.11 -m uvicorn app.api:app --host 0.0.0.0 --port {port} > /var/log/recant.log 2>&1 &
"""


def ensure_sg(ec2):
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    vpc_id = vpcs[0]["VpcId"]
    try:
        sg = ec2.create_security_group(
            GroupName=NAME, Description="recant demo console", VpcId=vpc_id
        )
        sg_id = sg["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": PORT, "ToPort": PORT,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "demo console"}]},
            ],
        )
        print(f"  security group {sg_id} created")
    except ClientError as e:
        if "InvalidGroup.Duplicate" not in str(e):
            raise
        sg_id = ec2.describe_security_groups(GroupNames=[NAME])["SecurityGroups"][0]["GroupId"]
        print(f"  security group {sg_id} reused")
    return sg_id


def latest_al2023(ssm):
    p = ssm.get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
    )
    return p["Parameter"]["Value"]


def teardown(ec2):
    r = ec2.describe_instances(
        Filters=[{"Name": "tag:Name", "Values": [NAME]},
                 {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]}]
    )
    ids = [i["InstanceId"] for res in r["Reservations"] for i in res["Instances"]]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        print(f"  terminating {ids}")
    else:
        print("  nothing running")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teardown", action="store_true")
    a = ap.parse_args()

    ec2 = boto3.client("ec2", region_name=REGION)
    if a.teardown:
        teardown(ec2)
        return

    db = os.environ["DATABASE_URL"]
    # strip the local CA path; the instance appends certifi's bundle in user-data
    db = db.split("&sslrootcert=")[0]

    sg_id = ensure_sg(ec2)
    ami = latest_al2023(boto3.client("ssm", region_name=REGION))
    print(f"  ami {ami}")

    ud = USER_DATA.format(repo=REPO, db=db, region=REGION, port=PORT)
    r = ec2.run_instances(
        ImageId=ami,
        InstanceType="t3.small",
        MinCount=1,
        MaxCount=1,
        SecurityGroupIds=[sg_id],
        UserData=ud,
        TagSpecifications=[{"ResourceType": "instance",
                            "Tags": [{"Key": "Name", "Value": NAME}]}],
        MetadataOptions={"HttpTokens": "required"},
    )
    iid = r["Instances"][0]["InstanceId"]
    print(f"  instance {iid} launching ...")

    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    d = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    host = d.get("PublicDnsName") or d.get("PublicIpAddress")
    url = f"http://{host}"
    print(f"  running at {url}\n  waiting for the app (model download takes a few minutes) ...")

    import urllib.error
    import urllib.request

    for i in range(60):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=8) as resp:
                if resp.status == 200:
                    print(f"\n  LIVE: {url}")
                    print(f"  {resp.read().decode()[:120]}")
                    return
        except Exception:
            pass
        time.sleep(15)
        if i % 4 == 0:
            print(f"    still booting ({(i + 1) * 15}s)")
    print(f"\n  timed out waiting. Instance is up at {url}")
    print("  check: aws ec2 get-console-output --instance-id " + iid)
    sys.exit(1)


if __name__ == "__main__":
    main()

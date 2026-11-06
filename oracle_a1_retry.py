#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OCI A1 1 OCPU / 6 GB retry helper. Python 3.9+, official OCI CLI only."""
import argparse
import configparser
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time
import uuid

SHAPE = "VM.Standard.A1.Flex"
TAG = "a1-retry-job"
FINAL_STATES = {"TERMINATED"}


class Stop(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__("{} {}: {}".format(status, code, message))


def log(message):
    print("[{}] {}".format(datetime.datetime.now().astimezone().isoformat(timespec="seconds"), message), flush=True)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temp), str(path))


def read_json(path):
    with open(str(path), encoding="utf-8") as handle:
        return json.load(handle)


def decode_json(text):
    try:
        return json.loads(text)
    except ValueError:
        pass
    for match in re.finditer(r"\{", text):
        try:
            return json.JSONDecoder().raw_decode(text[match.start():])[0]
        except ValueError:
            continue
    return None


class OCI:
    def __init__(self, region, profile=None):
        self.region, self.profile = region, profile
        if not shutil.which("oci"):
            raise Stop("未找到 OCI CLI。推荐在 Oracle 控制台的 Cloud Shell 中运行；本机运行需先安装并登录 OCI CLI。")

    def call(self, *args):
        # Reads can be retried independently; POSTs are controlled by persisted state.
        for attempt in range(4):
            try:
                return self._call_once(*args)
            except ApiError as error:
                if args[0] == "raw-request" or attempt == 3 or not (
                        error.status == 0 or error.status == 429 or error.status >= 500):
                    raise
                delay = 5 * (2 ** attempt)
                log("读取云资源暂时失败，{} 秒后重试。".format(delay))
                time.sleep(delay)

    def _call_once(self, *args):
        command = ["oci", "--region", self.region, "--output", "json", "--no-retry",
                   "--connection-timeout", "15", "--read-timeout", "75"]
        if self.profile:
            command += ["--profile", self.profile]
        command += list(args)
        env = os.environ.copy()
        env["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "True"
        # OCI CLI enables interactive prompting for any non-empty value,
        # including the string "False". Unset it for unattended subprocesses.
        env.pop("OCI_CLI_AUTO_PROMPT", None)
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=150, env=env)
        except subprocess.TimeoutExpired:
            raise ApiError(0, "TransportTimeout", "请求超时，结果可能未知；创建操作将保留原重试令牌。")
        body = decode_json(result.stdout)
        if isinstance(body, dict) and "status" in body:
            status = int(str(body["status"]).split()[0])
            data = body.get("data", {})
            if status >= 400:
                raise ApiError(status, data.get("code", "Unknown"), data.get("message", ""))
            if 200 <= status < 300:
                return data
        if result.returncode:
            error = decode_json(result.stderr) or body or {}
            if not isinstance(error, dict):
                error = {}
            raise ApiError(int(error.get("status", 0)), error.get("code", "CLIError"),
                           error.get("message", result.stderr.strip()[-1500:] or "OCI CLI 调用失败"))
        # OCI CLI can produce no stdout for --all on a collection with no items.
        # Accept this only for successful list commands, never for launch/POST.
        if len(args) >= 3 and args[2] == "list":
            if isinstance(body, list):
                return body
            if body is None and not result.stdout.strip():
                return []
        if not isinstance(body, dict) or "data" not in body:
            raise ApiError(0, "UnknownResponse", "响应不是预期的 OCI JSON；保留已有请求状态。")
        return body["data"]


def ask(label, default=""):
    answer = input(label + (" [{}]".format(default) if default else "") + ": ").strip()
    return answer or default


def pick(label, values, describe):
    if not values:
        raise Stop(label + "：没有找到可选项。请检查区域、区间和访问权限。")
    print("\n" + label)
    for index, value in enumerate(values, 1):
        print("  {}. {}".format(index, describe(value)))
    if len(values) == 1:
        print("自动选择唯一选项。")
        return values[0]
    while True:
        choice = ask("输入序号", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(values):
            return values[int(choice) - 1]


def subscriptions(oci, tenancy):
    rows = oci.call("iam", "region-subscription", "list", "--tenancy-id", tenancy, "--all")
    homes = [r["region-name"] for r in rows if r.get("is-home-region")]
    if len(homes) != 1 or oci.region != homes[0]:
        raise Stop("必须使用账户主区域；检测到的主区域：{}。".format(", ".join(homes) or "未知"))


def initialize(path, profile, subnet_id=None, ssh_key_file=None):
    if path.exists():
        raise Stop("配置已存在，未覆盖：{}".format(path))
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.path.expanduser(os.environ.get("OCI_CLI_CONFIG_FILE", "~/.oci/config")))
    settings = parser[profile or "DEFAULT"] if parser.has_section(profile or "DEFAULT") or not profile else {}
    region = ask("主区域，例如 ap-tokyo-1", os.environ.get("OCI_CLI_REGION") or settings.get("region", ""))
    if not re.fullmatch(r"[a-z]+-[a-z0-9]+-\d+", region):
        raise Stop("请填写 OCI 商业区域标识，例如 ap-tokyo-1。")
    tenancy = ask("租户 OCID（控制台个人菜单 → 租户详细信息）",
                  os.environ.get("OCI_TENANCY") or settings.get("tenancy", ""))
    if not tenancy.startswith("ocid1.tenancy."):
        raise Stop("租户 OCID 格式不正确。")
    oci = OCI(region, profile)
    subscriptions(oci, tenancy)
    compartment = ask("目标区间 OCID，直接回车使用根区间", tenancy)
    if subnet_id:
        if not ssh_key_file:
            raise Stop("指定 --subnet-id 时也需要 --ssh-public-key 公钥文件。")
        subnet = oci.call("network", "subnet", "get", "--subnet-id", subnet_id)
        primary = {"subnet-id": subnet_id, "nsg-ids": []}
        key = Path(ssh_key_file).expanduser().read_text(encoding="utf-8").strip()
    else:
        primary, key = reuse_instance(oci, compartment)
        subnet = oci.call("network", "subnet", "get", "--subnet-id", primary["subnet-id"])
    if "PRIVATE KEY" in key or not any(line.startswith(("ssh-", "ecdsa-", "sk-")) for line in key.splitlines()):
        raise Stop("需要 SSH 公钥，不能使用私钥。")
    images = oci.call("compute", "image", "list", "--compartment-id", compartment,
                      "--operating-system", "Oracle Linux", "--shape", SHAPE,
                      "--lifecycle-state", "AVAILABLE", "--sort-by", "TIMECREATED",
                      "--sort-order", "DESC", "--all")
    images = [x for x in images if not x.get("compartment-id")
              and str(x.get("operating-system-version", "")).split(".")[0] == "9"
              and "developer" not in x.get("display-name", "").lower()]
    if not images:
        raise Stop("没有找到 A1 兼容的 Oracle Linux 9 官方镜像。")
    image = images[0]
    ad_rows = oci.call("iam", "availability-domain", "list", "--compartment-id", tenancy, "--all")
    ads = [x["name"] for x in ad_rows if not subnet.get("availability-domain")
           or x["name"] == subnet["availability-domain"]]
    if subnet.get("prohibit-public-ip-on-vnic") or not ads:
        raise Stop("所选子网不允许公网 IP 或没有匹配的可用域。")
    config = {"region": region, "tenancy_id": tenancy, "compartment_id": compartment,
              "subnet_id": subnet["id"], "nsg_ids": primary.get("nsg-ids", []),
              "ssh_authorized_keys": key, "image_id": image["id"],
              "image_name": image["display-name"], "availability_domains": ads,
              "display_name": "Oracle-A1-1C6G", "job_id": str(uuid.uuid4()),
              "interval_seconds": 300, "max_attempts": 0, "profile": profile}
    write_json(path, config)
    log("已保存配置：{}；镜像：{}；可用域：{}".format(path, image["display-name"], ", ".join(ads)))
    log("规格固定 A1 / 1 OCPU / 6 GB / 50 GB 启动盘。此步骤没有创建云资源。")


def reuse_instance(oci, compartment):
    instances = oci.call("compute", "instance", "list", "--compartment-id", compartment, "--all")
    instances = [x for x in instances if x.get("lifecycle-state") not in {"TERMINATED", "TERMINATING"}]
    if not instances:
        raise Stop("此区间没有已有实例。请使用 --init --subnet-id 子网OCID --ssh-public-key 公钥文件，或调整目标区间。")
    source = pick("选择原虚机（只读取其网络和 SSH 公钥，不修改它）", instances,
                  lambda x: "{} / {} / {}".format(x["display-name"], x["shape"], x["lifecycle-state"]))
    vnics = oci.call("compute", "vnic-attachment", "list", "--compartment-id", compartment,
                     "--instance-id", source["id"], "--all")
    primary = None
    for attachment in vnics:
        if attachment.get("lifecycle-state") == "ATTACHED":
            vnic = oci.call("network", "vnic", "get", "--vnic-id", attachment["vnic-id"])
            if vnic.get("is-primary"):
                primary = vnic
                break
    if not primary or not primary.get("subnet-id"):
        raise Stop("未找到原虚机的主子网。")
    key = source.get("metadata", {}).get("ssh_authorized_keys", "").strip()
    if not key:
        key_path = Path(ask("原实例未保存 SSH 公钥，请输入 SSH 公钥 .pub 文件路径")).expanduser()
        key = key_path.read_text(encoding="utf-8").strip()
    return primary, key


def validate(config):
    for key in ("region", "tenancy_id", "compartment_id", "subnet_id", "image_id", "job_id",
                "display_name", "ssh_authorized_keys", "availability_domains"):
        if not config.get(key):
            raise Stop("配置缺少：" + key)
    if not re.fullmatch(r"[a-z]+-[a-z0-9]+-\d+", config["region"]):
        raise Stop("只支持 OCI 商业区域，例如 ap-tokyo-1。")
    if not isinstance(config["availability_domains"], list):
        raise Stop("availability_domains 必须是数组。")
    if "PRIVATE KEY" in config["ssh_authorized_keys"]:
        raise Stop("配置中不能使用私钥。")
    interval = config.get("interval_seconds", 300)
    attempts = config.get("max_attempts", 0)
    if not isinstance(interval, int) or interval < 120:
        raise Stop("interval_seconds 至少为 120 秒，默认 300 秒。")
    if not isinstance(attempts, int) or attempts < 0:
        raise Stop("max_attempts 必须是非负整数，0 表示持续重试。")


def find_existing(oci, config):
    rows = oci.call("compute", "instance", "list", "--compartment-id", config["compartment_id"], "--all")
    active = [x for x in rows if x.get("lifecycle-state") not in FINAL_STATES]
    for instance in active:
        if instance.get("display-name") == config["display_name"] and instance.get("freeform-tags", {}).get(TAG) != config["job_id"]:
            raise Stop("发现同名但不属于本任务的实例，已停止。请先核查控制台，避免误认或重复创建。")
    return [x for x in active if x.get("freeform-tags", {}).get(TAG) == config["job_id"]]


def budget_check(oci, config):
    """Conservative current-resource check, including stopped VMs and unattached disks."""
    subscriptions(oci, config["tenancy_id"])
    compartments = oci.call("iam", "compartment", "list", "--compartment-id", config["tenancy_id"],
                            "--compartment-id-in-subtree", "true", "--access-level", "ANY", "--all")
    ids = {config["tenancy_id"], config["compartment_id"]}
    ids.update(c["id"] for c in compartments if c.get("lifecycle-state") == "ACTIVE")
    ads = oci.call("iam", "availability-domain", "list", "--compartment-id", config["tenancy_id"], "--all")
    ocpus, memory, disks = 0.0, 0.0, 0
    for cid in sorted(ids):
        instances = oci.call("compute", "instance", "list", "--compartment-id", cid, "--all")
        for instance in instances:
            if instance.get("lifecycle-state") not in FINAL_STATES and instance.get("shape") == SHAPE:
                shape = instance.get("shape-config", {})
                if shape.get("ocpus") is None or shape.get("memory-in-gbs") is None:
                    raise Stop("无法读取现有 A1 配额，停止创建。")
                ocpus += float(shape["ocpus"])
                memory += float(shape["memory-in-gbs"])
        volumes = oci.call("bv", "volume", "list", "--compartment-id", cid, "--all")
        for ad in ads:
            volumes += oci.call("bv", "boot-volume", "list", "--compartment-id", cid,
                                "--availability-domain", ad["name"], "--all")
        disks += sum(int(v["size-in-gbs"]) for v in volumes
                     if v.get("lifecycle-state") != "TERMINATED")
    log("当前可见资源：A1 {} OCPU / {} GB，启动盘与块存储 {} GB。".format(ocpus, memory, disks))
    if ocpus + 1 > 2 or memory + 6 > 12 or disks + 50 > 200:
        raise Stop("新增后会超过本脚本采用的保守免费范围（A1 合计 2 OCPU/12 GB、磁盘合计 200 GB），已停止。")
    log("当前资源检查通过；历史月度用量和其他服务费用请仍以控制台为准。")


def make_request(config, ad):
    return {"availabilityDomain": ad, "compartmentId": config["compartment_id"],
            "displayName": config["display_name"], "shape": SHAPE,
            "shapeConfig": {"ocpus": 1, "memoryInGBs": 6},
            "sourceDetails": {"sourceType": "image", "imageId": config["image_id"],
                              "bootVolumeSizeInGBs": 50, "bootVolumeVpusPerGB": 10},
            "createVnicDetails": {"subnetId": config["subnet_id"], "assignPublicIp": True,
                                  "nsgIds": config.get("nsg_ids", [])},
            "metadata": {"ssh_authorized_keys": config["ssh_authorized_keys"]},
            "freeformTags": {TAG: config["job_id"]}}


def success(state_path, state, instance):
    state["instance_id"] = instance["id"]
    state.pop("pending", None)
    write_json(state_path, state)
    log("实例已创建/已存在，停止所有创建重试。")
    print("名称：{}\n实例 OCID：{}\n状态：{}".format(instance.get("display-name", instance.get("displayName", "")),
          instance["id"], instance.get("lifecycle-state", instance.get("lifecycleState", "PROVISIONING"))))
    print("请到控制台等待 RUNNING 并查看公网 IP；使用原虚机的 SSH 私钥和 opc 用户连接。")


def run(config_path, check_only=False):
    config = read_json(config_path)
    validate(config)
    oci = OCI(config["region"], config.get("profile"))
    state_path = config_path.with_suffix(".state.json")
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    state = read_json(state_path) if state_path.exists() else {"config_hash": digest, "attempts": 0, "ad_index": 0}
    if state.get("config_hash") != digest:
        raise Stop("配置与已有状态不一致。不要删除状态文件规避检查；先核查控制台和原任务。")
    if state.get("instance_id"):
        log("本任务已经成功，实例：{}；不会再次创建。".format(state["instance_id"]))
        return
    existing = find_existing(oci, config)
    if existing:
        if len(existing) != 1:
            raise Stop("发现多个同名或同任务实例，请在控制台核查，停止创建。")
        success(state_path, state, existing[0])
        return
    budget_check(oci, config)
    subnet = oci.call("network", "subnet", "get", "--subnet-id", config["subnet_id"])
    if subnet.get("prohibit-public-ip-on-vnic"):
        raise Stop("子网不允许公网 IP。")
    if subnet.get("availability-domain") and any(
            ad != subnet["availability-domain"] for ad in config["availability_domains"]):
        raise Stop("配置包含子网不支持的可用域。")
    log("计划：{}，A1 1 OCPU / 6 GB / 50 GB；正常重试间隔 {} 秒，加随机等待。".format(
        config["region"], config.get("interval_seconds", 300)))
    if check_only:
        log("只读检查结束，未发送创建请求。")
        return
    write_json(state_path, state)
    request_path = config_path.with_suffix(".request.json")
    transient_count = 0
    checked_at = time.monotonic()
    while True:
        if config.get("max_attempts", 0) and state["attempts"] >= config["max_attempts"]:
            log("已达到 max_attempts，保留状态并退出；重新运行可检查是否已创建。")
            return
        # Reconcile before each POST, including restarts and uncertain results.
        existing = find_existing(oci, config)
        if existing:
            if len(existing) != 1:
                raise Stop("发现多个同名或同任务实例，停止创建。")
            success(state_path, state, existing[0])
            return
        if time.monotonic() - checked_at >= 3600:
            budget_check(oci, config)
            checked_at = time.monotonic()
        if not state.get("pending"):
            ad = config["availability_domains"][state["ad_index"] % len(config["availability_domains"])]
            state["pending"] = {"token": str(uuid.uuid4()), "created": time.time(),
                                "body": make_request(config, ad)}
            write_json(state_path, state)
        pending = state["pending"]
        if time.time() - pending["created"] >= 20 * 3600:
            raise Stop("同一请求令牌已接近 24 小时有效期。为避免结果未知时重复创建，已停止；请先人工核查控制台。")
        write_json(request_path, pending["body"])
        state["attempts"] += 1
        write_json(state_path, state)
        log("第 {} 次请求，可用域 {}。".format(state["attempts"], pending["body"]["availabilityDomain"]))
        try:
            instance = oci.call("raw-request", "--http-method", "POST", "--target-uri",
                                "https://iaas.{}.oraclecloud.com/20160918/instances".format(config["region"]),
                                "--request-headers", json.dumps({"opc-retry-token": pending["token"],
                                                                 "Content-Type": "application/json"}),
                                "--request-body", "file://" + str(request_path.resolve()))
            if not isinstance(instance, dict) or not instance.get("id"):
                raise ApiError(0, "UnknownResponse", "创建响应缺少实例 ID，保留原令牌。")
            success(state_path, state, instance)
            return
        except ApiError as error:
            text = error.message.lower()
            if error.status >= 500 and ("out of host capacity" in text or "out of capacity" in text):
                # Explicit refusal: safe to try the next AD with a new token.
                state.pop("pending", None)
                state["ad_index"] += 1
                transient_count = 0
                log("容量不足，稍后重试。")
            elif error.status == 429 or error.status >= 500 or error.status == 0:
                # Ambiguous timeout/server failure: never switch body or retry token.
                transient_count += 1
                log("临时错误 {} {}，保留原请求令牌，退避后核查。".format(error.status, error.code))
            else:
                raise Stop("{}；权限、配额、镜像或参数错误不属于缺货，已停止。".format(error))
        write_json(state_path, state)
        delay = min(config.get("interval_seconds", 300) * (2 ** min(transient_count, 4)), 1800)
        delay += random.randint(0, 30)
        log("等待 {} 秒。按 Ctrl+C 停止；下次运行会读取已有状态。".format(delay))
        time.sleep(delay)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="申请 Oracle A1 1 OCPU / 6 GB，容量不足时低频重试")
    parser.add_argument("--config", default="a1-config.json", help="配置与任务状态放在同一目录")
    parser.add_argument("--profile", help="初始化时使用的 OCI CLI profile；Cloud Shell 通常不需要")
    parser.add_argument("--subnet-id", help="初始化时直接使用已有公网子网，无需原虚机")
    parser.add_argument("--ssh-public-key", help="与 --subnet-id 一起使用的 SSH 公钥 .pub 文件")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true", help="交互生成配置，只读取云资源")
    group.add_argument("--check", action="store_true", help="只做只读检查，不申请实例")
    args = parser.parse_args()
    if (args.subnet_id or args.ssh_public_key) and not args.init:
        parser.error("--subnet-id 和 --ssh-public-key 只能与 --init 一起使用")
    path = Path(args.config).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # OS lock is automatically released even after interruption. Keep this file.
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("同一配置已有脚本运行，不能同时启动。")
        if args.init:
            initialize(path, args.profile or os.environ.get("OCI_CLI_PROFILE"), args.subnet_id, args.ssh_public_key)
        else:
            run(path, args.check)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已停止；保留请求状态，下次可继续。")
        sys.exit(130)
    except (Stop, ApiError, OSError, ValueError, KeyError, EOFError) as error:
        print("停止：{}".format(error), file=sys.stderr)
        sys.exit(1)

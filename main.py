import json
import time
import argparse
import os
import logging
import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("reserve.log", encoding="utf-8"),
    ],
)


import requests
from email.utils import parsedate_to_datetime
from statistics import median

from utils import reserve, get_user_credentials

# 超星 Date 头相对本机的秒数（正=超星快于本机）。开火按这台钟，不按 GitHub 墙钟。
_CX_OFFSET = 0.0


def sync_chaoxing_clock(rounds=3):
    """安师大 exe server_offset：3 次 Date 头取中位数。不跟随跳转，避免绕到 cxlowcode 拖时间。"""
    global _CX_OFFSET
    samples = []
    for _ in range(rounds):
        t0 = time.time()
        try:
            resp = requests.get(
                "https://office.chaoxing.com/",
                timeout=5,
                verify=False,
                allow_redirects=False,
            )
        except requests.RequestException as err:
            logging.warning(f"对超星钟失败: {err}")
            continue
        t1 = time.time()
        hdr = resp.headers.get("Date")
        if not hdr:
            continue
        srv = parsedate_to_datetime(hdr)
        if srv.tzinfo is None:
            srv = srv.replace(tzinfo=datetime.timezone.utc)
        samples.append(srv.timestamp() - (t0 + t1) / 2)
    if samples:
        _CX_OFFSET = float(median(samples))
    logging.info(
        f"超星钟偏差 {_CX_OFFSET:+.3f}s （正=超星快于本机，样本 {len(samples)}）"
    )
    return _CX_OFFSET


def beijing_now():
    return datetime.datetime.utcfromtimestamp(
        time.time() + _CX_OFFSET
    ) + datetime.timedelta(hours=8)


def get_current_time(_action=False):
    return beijing_now().strftime("%H:%M:%S")


def get_current_dayofweek(_action=False):
    return beijing_now().strftime("%A")


RUN_ONCE = True
SLEEPTIME = 0.1  # 安师大 exe sleep_time
ENDTIME = "20:02:00"
OPEN_TIME = "20:00:00"
SPRINT_LEAD_MS = 300  # 开放前 0.3 秒开火
SPRINT_PREP_SECONDS = 4  # 等到 t0-4 秒才开始做第一座的包
RUN_SECONDS = 6  # 狂刷时长
RESERVE_TIME = "20:00:00"
LOGIN_LEAD_SECONDS = 20  # 对齐 exe：约 19:59:40 登录，再等到 t0-4 做包

ENABLE_SLIDER = True
MAX_ATTEMPT = 1
RESERVE_NEXT_DAY = True
POST_LOGIN_DELAY = 0.0
RETRY_INTERVAL = 15.0


def precise_sleep_until(ts):
    """与安师大 exe Reserver.precise_sleep_until 相同：>0.3s 睡 remain-0.2，最后忙等。"""
    while True:
        remain = ts - time.time()
        if remain <= 0:
            return
        if remain > 0.3:
            time.sleep(remain - 0.2)


def exe_fire_timestamps():
    """安师大: t0_local = open - offset, fire = t0_local - lead_ms/1000."""
    bj = beijing_now()
    hh, mm, ss = [int(x) for x in OPEN_TIME.split(":")[:3]]
    open_dt = datetime.datetime(
        bj.year, bj.month, bj.day, hh, mm, ss,
        tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
    )
    t0_local = open_dt.timestamp() - _CX_OFFSET
    fire_at = t0_local - (SPRINT_LEAD_MS / 1000.0)
    prep_at = t0_local - SPRINT_PREP_SECONDS
    return t0_local, fire_at, prep_at


def exe_sprint_reserve(client, times, roomid, seatid, action, fire_at, prep_at):
    """安师大 exe run() 冲刺：等到 t0-4 做第一座包 → 等到开火 → 预热包只用一次 → 其余现场做包。"""
    seats = client._normalize_seat_ids(seatid)
    day_delta = 1 if client.reserve_next_day else 0
    day = (
        datetime.datetime.utcnow() + datetime.timedelta(hours=8, days=day_delta)
    ).strftime("%Y-%m-%d")
    logging.info(
        f"目标 {day}  房间 {roomid}  座位 {seats}  时间 {times[0]}-{times[1]}"
    )
    if time.time() < prep_at:
        precise_sleep_until(prep_at)
    prewarm = None
    first = seats[0]
    try:
        pkt = client.prepare_packet(times, roomid, first, action)
        if pkt:
            logging.info(f"预热完成：座位 {first} 的 token/验证码已备好")
            prewarm = pkt
        else:
            logging.warning("预热失败（忽略，改为实时解）：没有 token")
    except Exception as err:
        logging.warning(f"预热失败（忽略，改为实时解）：{err}")
    precise_sleep_until(fire_at)
    logging.info("★ 开始冲刺狂刷 ★")
    deadline = time.time() + RUN_SECONDS
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        for seat in seats:
            try:
                if prewarm and prewarm.get("seat") == seat:
                    ok, _msg = client.fire_packet(prewarm, action, f"第{attempt}轮")
                    prewarm = None
                else:
                    fresh = client.prepare_packet(times, roomid, seat, action)
                    ok, _msg = client.fire_packet(fresh, action, f"第{attempt}轮")
                if ok:
                    logging.info(f"预约成功！座位 {seat}  {times[0]}-{times[1]}  {day}")
                    logging.info(f"完成，共尝试 {attempt} 轮")
                    return True
            except Exception as err:
                logging.warning(f"座位 {seat} 尝试异常：{err}")
        time.sleep(SLEEPTIME)
    logging.error(
        f"在 {RUN_SECONDS}s 内未成功，共 {attempt} 轮。请检查座位号/时间/是否已到开放时间"
    )
    return False


def create_reserve_clients(user_count):
    clients = []
    for _ in range(user_count):
        client = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        client.warm_up()
        clients.append(client)
    return clients


def login_user(client, username, password):
    if client.logged_in:
        return True
    client.get_login_status()
    login_success, msg = client.login(username, password)
    if not login_success:
        logging.info(f"登录失败: {msg}")
        return False
    return True


def login_and_reserve(
    users,
    usernames,
    passwords,
    action,
    success_list=None,
    clients=None,
):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    if action and len(usernames.split(",")) != len(users):
        raise Exception("user number should match the number of config")
    if success_list is None:
        success_list = [False] * len(users)
    if clients is None:
        clients = create_reserve_clients(len(users))
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        if not success_list[index]:
            logging.info(
                f"----------- {username} -- {times} -- {seatid} try -----------"
            )
            s = clients[index]
            if not s.logged_in:
                s.get_login_status()
                login_success, _ = s.login(username, password)
                if not login_success:
                    continue
                logging.info(
                f"登录后等待 {POST_LOGIN_DELAY} 秒再预约"
                )
                time.sleep(POST_LOGIN_DELAY)
            now = time.time()
            suc = exe_sprint_reserve(s, times, roomid, seatid, action, now, now)
            success_list[index] = suc
    return success_list, clients


def main(users, action=False):
    # 1. 第一步：如果是 GitHub Action，先把账号密码从环境变量里拿出来
    # 这一步要在八点前做完，不能等八点到了才现拿
    usernames, passwords = None, None
    clients = None
    if action:
        usernames, passwords = get_user_credentials(action)
        if not usernames or not passwords:
            raise Exception("USERNAMES/PASSWORDS secrets missing")

        logging.info("GitHub Action 已启动，按安师大 exe 冲刺：lead=300ms run=6s sleep=0.1s")
        # OpenCV/连接放在 19:20 做完，不能占开火前那几秒
        clients = create_reserve_clients(len(users))
        sync_chaoxing_clock()
        t0_local, fire_at, prep_at = exe_fire_timestamps()
        logging.info(
            f"冲刺模式：开放 {OPEN_TIME}，服务器时钟偏差 {_CX_OFFSET:+.2f}s，"
            f"将于本机 {datetime.datetime.fromtimestamp(fire_at).strftime('%H:%M:%S.%f')[:-3]} 开火"
        )
        # 对齐 exe 截图：约 19:59:42 登录，不是等到做包前才登录
        precise_sleep_until(t0_local - LOGIN_LEAD_SECONDS)
        logging.info("开始登录...")
        for index, user in enumerate(users):
            username, password = (
                usernames.split(",")[index].strip(),
                passwords.split(",")[index].strip(),
            )
            if not login_user(clients[index], username, password):
                logging.error(f"登录失败 index={index}")
        # 登录后再对钟，和 exe 一样
        sync_chaoxing_clock()
        t0_local, fire_at, prep_at = exe_fire_timestamps()
        logging.info(
            f"冲刺模式：开放 {OPEN_TIME}，服务器时钟偏差 {_CX_OFFSET:+.2f}s，"
            f"将于本机 {datetime.datetime.fromtimestamp(fire_at).strftime('%H:%M:%S.%f')[:-3]} 开火"
        )

    if clients is None:
        clients = create_reserve_clients(len(users))

    current_time = get_current_time(action)
    logging.info(f"start time {current_time}, action {'on' if action else 'off'}")
    success_list = [False] * len(users)
    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )

    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if action:
            username, password = (
                usernames.split(",")[index].strip(),
                passwords.split(",")[index].strip(),
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        if not login_user(clients[index], username, password):
            continue
        if action:
            success_list[index] = exe_sprint_reserve(
                clients[index], times, roomid, seatid, action, fire_at, prep_at
            )
        else:
            now = time.time()
            success_list[index] = exe_sprint_reserve(
                clients[index], times, roomid, seatid, action, now, now
            )

    print(f"time now {current_time}, success list {success_list}")
    if success_list and sum(success_list) == today_reservation_num:
        print("reserved successfully!")
        return
    return

def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    suc = False
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.warm_up()
        s.get_login_status()
        s.login(username, password)
        suc = s.submit(times, roomid, seatid, action)
        if suc:
            return


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.warm_up()
    s.get_login_status()
    s.login(username=username, password=password)
    encode = input("请输入deptldEnc：")
    s.roomid(encode)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)

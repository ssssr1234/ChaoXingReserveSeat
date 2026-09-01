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

# 超星 Date 头相对本机的秒数（正=超星比本机快）。开火按这台钟，不按 GitHub 墙钟。
_CX_OFFSET = 0.0


def sync_chaoxing_clock(rounds=7):
    global _CX_OFFSET
    samples = []
    for _ in range(rounds):
        t0 = time.time()
        try:
            resp = requests.get(
                "https://office.chaoxing.com/", timeout=5, verify=False
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
SLEEPTIME = 0.2  # 每次抢座的间隔
ENDTIME = "20:02:00"  # 根据学校的预约座位时间+1min即可
RESERVE_TIME = "20:00:01"  # 超星钟：开门后 1 秒开火（对齐已约上的 01 打 02 中）
PREWARM_LEAD_SECONDS = 40  # 开火前完成环境预热+登录
PACKET_LEAD_SECONDS = 14  # 开火前开始做第一枪预热包

ENABLE_SLIDER = True  # 是否有滑块验证
MAX_ATTEMPT = 1  # 最大尝试次数
RESERVE_NEXT_DAY = True  # 预约明天而不是今天的
POST_LOGIN_DELAY = 0.0
RETRY_INTERVAL = 15.0    # 整批失败后等待15秒


def wait_until_fire(lead_seconds=0):
    """Block until Beijing clock reaches RESERVE_TIME minus lead_seconds."""
    fire_clock = datetime.datetime.strptime(RESERVE_TIME, "%H:%M:%S").time()
    while True:
        now = beijing_now()
        target = datetime.datetime.combine(now.date(), fire_clock)
        if (now - target).total_seconds() > 12 * 3600:
            target += datetime.timedelta(days=1)
        remaining = (target - now).total_seconds() - lead_seconds
        if remaining <= 0:
            return now, (target - now).total_seconds()
        time.sleep(1 if remaining > 10 else 0.01)


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


def primary_seat(seatid):
    if isinstance(seatid, (list, tuple)):
        return str(seatid[0]).strip()
    return str(seatid).strip()


def login_user(client, username, password):
    if client.logged_in:
        return True
    client.get_login_status()
    login_success, msg = client.login(username, password)
    if not login_success:
        logging.info(f"登录失败: {msg}")
        return False
    return True


def two_shot_reserve(client, times, roomid, seatid, action, prepared_packet=None):
    seat = primary_seat(seatid)
    logging.info(f"两枪策略 座位 {seat} 第一枪预热包丢掉，第二枪现场做包")

    packet1 = prepared_packet
    if packet1 is None:
        logging.warning("开火时预热包未就绪，跳过第一枪，避免现场做包拖到 15 秒")
    else:
        ok1, msg1 = client.fire_packet(packet1, action, "第一枪")
        packet1 = None
        if ok1:
            logging.info("第一枪预约成功")
            return True
        logging.info(f"第一枪结束 msg={msg1}，丢掉预热包，现场做第二枪")

    packet2 = client.prepare_packet(times, roomid, seat, action)
    ok2, msg2 = client.fire_packet(packet2, action, "第二枪")
    if ok2:
        logging.info("第二枪预约成功")
        return True
    logging.info(f"第二枪未成功 msg={msg2}")
    return False


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
            suc = two_shot_reserve(s, times, roomid, seatid, action)
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

        logging.info(
            f"GitHub Action 已启动，第一枪对准超星钟 {RESERVE_TIME}"
        )
        prepared = {}
        sync_chaoxing_clock()

        wait_until_fire(PREWARM_LEAD_SECONDS)
        sync_chaoxing_clock()
        logging.info("开始预约前预热并登录...")
        clients = create_reserve_clients(len(users))
        for index, user in enumerate(users):
            username, password = (
                usernames.split(",")[index].strip(),
                passwords.split(",")[index].strip(),
            )
            if not login_user(clients[index], username, password):
                logging.error(f"预热登录失败 index={index}")

        wait_until_fire(PACKET_LEAD_SECONDS)
        for index, user in enumerate(users):
            if not clients[index].logged_in:
                continue
            _username, _password, times, roomid, seatid, _days = user.values()
            seat = primary_seat(seatid)
            logging.info(f"开始准备第一枪预热包 座位 {seat}")
            prepared[index] = clients[index].prepare_packet(
                times, roomid, seat, action
            )

        now, remaining = wait_until_fire(0)
        wall = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        logging.info(
            f"到达开火 超星钟={now.strftime('%H:%M:%S.%f')[:-3]} "
            f"墙钟={wall.strftime('%H:%M:%S.%f')[:-3]} "
            f"offset={_CX_OFFSET:+.3f}s remaining={remaining:.3f}s，打第一枪"
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
        success_list[index] = two_shot_reserve(
            clients[index],
            times,
            roomid,
            seatid,
            action,
            prepared_packet=prepared.get(index) if action else None,
        )

    print(f"time now {current_time}, success list {success_list}")
    if success_list and sum(success_list) == today_reservation_num:
        print("reserved successfully!")
        return
    logging.info("两枪结束")
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

#!/usr/bin/env bash
# ROS2 환경 설정. relay_map.py / relay_charge.py 가 이 스크립트를 source 한 뒤 실행된다.
#
# 프로젝트 주석은 /opt/robot/scripts/setup_ros2.sh 를 가리키지만 실제 장비에는 없어서
# 이 파일로 대체한다. 설치 위치도 /opt/robot/scripts/ 로 맞춰 둔다.
#
# relay_map.py   : nav_msgs, sensor_msgs (ROS2 기본)
# relay_charge.py: drdds.msg (커스텀 메시지 -> 워크스페이스 필요)

ROS_DISTRO_SETUP="${ROS_DISTRO_SETUP:-/opt/ros/humble/setup.bash}"
WS_SETUP="${WS_SETUP:-/home/und02/openarmx_ws/install/setup.bash}"

# shellcheck disable=SC1090
if [ -f "$ROS_DISTRO_SETUP" ]; then
    source "$ROS_DISTRO_SETUP"
else
    echo "[setup_ros2] ROS2 setup 을 찾지 못함: $ROS_DISTRO_SETUP" >&2
fi

# 워크스페이스는 커스텀 메시지(drdds 등)를 제공한다. 없으면 relay_charge 가 import 에서 죽는다.
if [ -f "$WS_SETUP" ]; then
    source "$WS_SETUP"
fi

#!/bin/sh
cmd=`realpath $0`
_DIR=`dirname $cmd`
cd $_DIR

udevadm control --stop-exec-queue

__insmod()
{
	if [ -f "$1" ];then
		insmod $@
	fi
}

__rmmod_camera_sensor()
{
	for item in `echo "imx415 os04a10 sc4336 sc3336 sc530ai gc2053 sc200ai sc401ai sc450ai techpoint"`
	do
		if lsmod | grep $item | awk '{print $3}' |grep -w 0;then
			rmmod $item
		fi
	done
}

__chk_camera_sensor_height()
{
	# TODO: Do Not Support dual Camera Sensor
	sensor_height=0
	for item in `echo "/proc/rkisp-vir0 /proc/rkisp0"`
	do
		if grep -w "Input.*Format" $item; then
			msg_sen=`grep -w "Input.*Format" $item`
			msg_sen=${msg_sen##*Size:*x}
			msg_sen=${msg_sen%%@*}
			sensor_height="$msg_sen"
			break
		fi
	done
}

__insmod rtc-pcf8563.ko

__insmod rk_dvbm.ko

__insmod videobuf2-memops.ko
__insmod videobuf2-common.ko
__insmod videobuf2-v4l2.ko
__insmod videobuf2-vmalloc.ko
__insmod videobuf2-cma-sg.ko

# CAM DRV CHOOSE
#__insmod sc431hai.ko
__insmod sc235hai.ko

__insmod techpoint.ko

__insmod video_rkcif.ko
__insmod video_rkisp.ko
__insmod phy-rockchip-csi2-dphy-hw.ko
__insmod phy-rockchip-csi2-dphy.ko

__rmmod_camera_sensor

echo 1 > /sys/module/video_rkcif/parameters/clr_unready_dev
echo 1 > /sys/module/video_rkisp/parameters/clr_unready_dev

__insmod rga3.ko

__insmod mpp_vcodec.ko

__insmod rknpu.ko
__insmod snd-soc-rv1106.ko

__insmod motor.ko

__chk_camera_sensor_height

__insmod rockit.ko mcu_fw_path="./hpmcu_wrap.bin" mcu_fw_addr=0xff6fe000 isp_max_h=$sensor_height

__insmod rve.ko

__insmod goodix.ko

udevadm control --start-exec-queue

# insmod wifi driver background
rm -f /run/wifi-load.failed
(
	"$(pwd)/insmod_wifi.sh"
	wifi_rc=$?
	if [ "$wifi_rc" -ne 0 ]; then
		printf 'exit_code=%s\n' "$wifi_rc" > /run/wifi-load.failed
		printf 'aicpm: Wi-Fi module load failed: exit_code=%s\n' "$wifi_rc" > /dev/kmsg
	fi
	exit "$wifi_rc"
) &

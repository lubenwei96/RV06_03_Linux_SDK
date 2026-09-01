/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_LINUX_AICPM_L9110S_H
#define _UAPI_LINUX_AICPM_L9110S_H

#include <linux/ioctl.h>
#include <linux/types.h>

#define AICPM_L9110S_ABI_VERSION 1U

enum aicpm_l9110s_direction {
	AICPM_L9110S_STOPPED = 0,
	AICPM_L9110S_FORWARD = 1,
	AICPM_L9110S_REVERSE = 2,
};

struct aicpm_l9110s_run {
	__u32 abi_version;
	__u32 direction;
	__u32 duty_permille;
	__u32 duration_ms;
};

struct aicpm_l9110s_status {
	__u32 abi_version;
	__u32 direction;
	__s32 last_error;
	__u32 remaining_ms;
};

#define AICPM_L9110S_IOC_MAGIC 'L'
#define AICPM_L9110S_IOC_RUN \
	_IOW(AICPM_L9110S_IOC_MAGIC, 0x01, struct aicpm_l9110s_run)
#define AICPM_L9110S_IOC_STOP \
	_IO(AICPM_L9110S_IOC_MAGIC, 0x02)
#define AICPM_L9110S_IOC_GET_STATUS \
	_IOR(AICPM_L9110S_IOC_MAGIC, 0x03, struct aicpm_l9110s_status)

#endif

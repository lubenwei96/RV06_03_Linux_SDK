/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _AICPM_L9110S_CORE_H
#define _AICPM_L9110S_CORE_H

#include <linux/mutex.h>
#include <linux/types.h>
#include <uapi/linux/aicpm_l9110s.h>

struct aicpm_l9110s_core_ops {
	int (*stop)(void *context);
	int (*drive)(void *context, u32 direction, u32 duty_permille);
	void (*dead_time)(void *context, u32 dead_time_us);
	void (*cancel_timeout_sync)(void *context);
	void (*schedule_timeout)(void *context, u32 duration_ms, u64 generation);
	u64 (*now_ms)(void *context);
};

struct aicpm_l9110s_core {
	struct mutex state_lock;
	struct mutex op_lock;
	const struct aicpm_l9110s_core_ops *ops;
	void *context;
	u32 max_run_time_ms;
	u32 dead_time_us;
	u32 direction;
	s32 last_error;
	u64 generation;
	u64 deadline_ms;
	s32 fault_errno;
	bool opened;
	bool dying;
	bool fault_latched;
};

enum aicpm_l9110s_output {
	AICPM_L9110S_OUTPUT_NONE = 0,
	AICPM_L9110S_OUTPUT_A,
	AICPM_L9110S_OUTPUT_B,
};

struct aicpm_l9110s_pair_ops {
	int (*apply)(void *context, enum aicpm_l9110s_output output,
		     u32 duty_permille);
};

struct aicpm_l9110s_pair {
	const struct aicpm_l9110s_pair_ops *ops;
	void *context;
	enum aicpm_l9110s_output active_output;
};

void aicpm_l9110s_pair_init(struct aicpm_l9110s_pair *pair,
			    const struct aicpm_l9110s_pair_ops *ops,
			    void *context);
int aicpm_l9110s_pair_stop(struct aicpm_l9110s_pair *pair);
int aicpm_l9110s_pair_drive(struct aicpm_l9110s_pair *pair,
			    u32 direction, u32 duty_permille);

int aicpm_l9110s_core_init(struct aicpm_l9110s_core *core,
			  const struct aicpm_l9110s_core_ops *ops,
			  void *context, u32 dead_time_us,
			  u32 max_run_time_ms);
int aicpm_l9110s_open_transaction(struct aicpm_l9110s_core *core);
void aicpm_l9110s_close_transaction(struct aicpm_l9110s_core *core);
int aicpm_l9110s_run_transaction(struct aicpm_l9110s_core *core,
				 const struct aicpm_l9110s_run *run);
int aicpm_l9110s_stop_transaction(struct aicpm_l9110s_core *core, int error);
int aicpm_l9110s_begin_remove_transaction(struct aicpm_l9110s_core *core,
					  int error);
int aicpm_l9110s_check_live_transaction(struct aicpm_l9110s_core *core);
int aicpm_l9110s_timeout_core(struct aicpm_l9110s_core *core, u64 generation);
void aicpm_l9110s_get_status_core(struct aicpm_l9110s_core *core,
				 struct aicpm_l9110s_status *status);
int aicpm_l9110s_get_status_transaction(struct aicpm_l9110s_core *core,
					struct aicpm_l9110s_status *status);

#endif

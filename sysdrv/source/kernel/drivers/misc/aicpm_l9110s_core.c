// SPDX-License-Identifier: GPL-2.0
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/limits.h>
#include <linux/string.h>

#include "aicpm_l9110s_core.h"

static void aicpm_l9110s_set_stopped_locked(struct aicpm_l9110s_core *core,
					    int reason, int hardware_error)
{
	core->direction = AICPM_L9110S_STOPPED;
	core->deadline_ms = 0;
	if (hardware_error && !core->fault_latched) {
		core->fault_latched = true;
		core->fault_errno = hardware_error;
	}
	core->last_error = core->fault_latched ? core->fault_errno : reason;
}

static int aicpm_l9110s_stop_hardware(struct aicpm_l9110s_core *core,
				      int reason)
{
	int hardware_error;
	int result;

	hardware_error = core->ops->stop(core->context);
	mutex_lock(&core->state_lock);
	aicpm_l9110s_set_stopped_locked(core, reason, hardware_error);
	result = core->fault_latched ? core->fault_errno : hardware_error;
	mutex_unlock(&core->state_lock);

	return result;
}

int aicpm_l9110s_core_init(struct aicpm_l9110s_core *core,
			  const struct aicpm_l9110s_core_ops *ops,
			  void *context, u32 dead_time_us,
			  u32 max_run_time_ms)
{
	if (!core || !ops || !ops->stop || !ops->drive || !ops->dead_time ||
	    !ops->cancel_timeout_sync || !ops->schedule_timeout || !ops->now_ms ||
	    !dead_time_us || !max_run_time_ms)
		return -EINVAL;
	if (dead_time_us > 100000 || max_run_time_ms > 30000)
		return -ERANGE;

	memset(core, 0, sizeof(*core));
	mutex_init(&core->state_lock);
	mutex_init(&core->op_lock);
	core->ops = ops;
	core->context = context;
	core->dead_time_us = dead_time_us;
	core->max_run_time_ms = max_run_time_ms;
	core->direction = AICPM_L9110S_STOPPED;

	return 0;
}

int aicpm_l9110s_open_transaction(struct aicpm_l9110s_core *core)
{
	int result = 0;

	mutex_lock(&core->op_lock);
	mutex_lock(&core->state_lock);
	if (core->dying)
		result = -ENODEV;
	else if (core->opened)
		result = -EBUSY;
	else
		core->opened = true;
	mutex_unlock(&core->state_lock);
	mutex_unlock(&core->op_lock);

	return result;
}

void aicpm_l9110s_close_transaction(struct aicpm_l9110s_core *core)
{
	bool dying;

	mutex_lock(&core->op_lock);
	mutex_lock(&core->state_lock);
	core->opened = false;
	dying = core->dying;
	mutex_unlock(&core->state_lock);
	if (dying)
		goto out_unlock;
	core->ops->cancel_timeout_sync(core->context);
	aicpm_l9110s_stop_hardware(core, -ECANCELED);
out_unlock:
	mutex_unlock(&core->op_lock);
}

static int aicpm_l9110s_validate_run(struct aicpm_l9110s_core *core,
				     const struct aicpm_l9110s_run *run)
{
	if (run->abi_version != AICPM_L9110S_ABI_VERSION)
		return -EINVAL;
	if (run->direction != AICPM_L9110S_FORWARD &&
	    run->direction != AICPM_L9110S_REVERSE)
		return -EINVAL;
	if (!run->duty_permille || run->duty_permille > 1000)
		return -EINVAL;
	if (run->duration_ms == 0)
		return -EINVAL;
	if (run->duration_ms > core->max_run_time_ms)
		return -ERANGE;
	return 0;
}

int aicpm_l9110s_run_transaction(struct aicpm_l9110s_core *core,
				 const struct aicpm_l9110s_run *run)
{
	u64 generation;
	int cleanup_error;
	int result;

	mutex_lock(&core->op_lock);
	mutex_lock(&core->state_lock);
	if (core->dying)
		result = -ENODEV;
	else if (core->fault_latched)
		result = core->fault_errno;
	else
		result = 0;
	mutex_unlock(&core->state_lock);
	if (result)
		goto out_unlock;

	result = aicpm_l9110s_validate_run(core, run);
	if (result)
		goto out_unlock;

	core->ops->cancel_timeout_sync(core->context);
	result = core->ops->stop(core->context);
	if (result) {
		cleanup_error = core->ops->stop(core->context);
		(void)cleanup_error;
		mutex_lock(&core->state_lock);
		aicpm_l9110s_set_stopped_locked(core, 0, result);
		mutex_unlock(&core->state_lock);
		goto out_unlock;
	}
	core->ops->dead_time(core->context, core->dead_time_us);
	result = core->ops->drive(core->context, run->direction,
				  run->duty_permille);
	if (result) {
		cleanup_error = core->ops->stop(core->context);
		(void)cleanup_error;
		mutex_lock(&core->state_lock);
		aicpm_l9110s_set_stopped_locked(core, 0, result);
		mutex_unlock(&core->state_lock);
		goto out_unlock;
	}

	mutex_lock(&core->state_lock);
	core->direction = run->direction;
	core->last_error = 0;
	core->generation++;
	core->deadline_ms = core->ops->now_ms(core->context) + run->duration_ms;
	generation = core->generation;
	mutex_unlock(&core->state_lock);
	core->ops->schedule_timeout(core->context, run->duration_ms, generation);
out_unlock:
	mutex_unlock(&core->op_lock);
	return result;
}

int aicpm_l9110s_stop_transaction(struct aicpm_l9110s_core *core, int error)
{
	int result;

	mutex_lock(&core->op_lock);
	mutex_lock(&core->state_lock);
	result = core->dying ? -ENODEV : 0;
	mutex_unlock(&core->state_lock);
	if (result)
		goto out_unlock;
	core->ops->cancel_timeout_sync(core->context);
	result = aicpm_l9110s_stop_hardware(core, error);
out_unlock:
	mutex_unlock(&core->op_lock);
	return result;
}

int aicpm_l9110s_begin_remove_transaction(struct aicpm_l9110s_core *core,
					  int error)
{
	int result;

	mutex_lock(&core->op_lock);
	mutex_lock(&core->state_lock);
	core->dying = true;
	mutex_unlock(&core->state_lock);
	core->ops->cancel_timeout_sync(core->context);
	result = aicpm_l9110s_stop_hardware(core, error);
	mutex_unlock(&core->op_lock);

	return result;
}

int aicpm_l9110s_check_live_transaction(struct aicpm_l9110s_core *core)
{
	int result;

	mutex_lock(&core->op_lock);
	mutex_lock(&core->state_lock);
	result = core->dying ? -ENODEV : 0;
	mutex_unlock(&core->state_lock);
	mutex_unlock(&core->op_lock);
	return result;
}

int aicpm_l9110s_timeout_core(struct aicpm_l9110s_core *core, u64 generation)
{
	int hardware_error;

	mutex_lock(&core->state_lock);
	if (generation != core->generation || core->dying ||
	    core->direction == AICPM_L9110S_STOPPED) {
		mutex_unlock(&core->state_lock);
		return 0;
	}
	hardware_error = core->ops->stop(core->context);
	aicpm_l9110s_set_stopped_locked(core, -ETIMEDOUT, hardware_error);
	mutex_unlock(&core->state_lock);
	return hardware_error;
}

void aicpm_l9110s_get_status_core(struct aicpm_l9110s_core *core,
				 struct aicpm_l9110s_status *status)
{
	u64 now;
	u64 remaining = 0;

	mutex_lock(&core->state_lock);
	now = core->ops->now_ms(core->context);
	if (core->deadline_ms > now)
		remaining = core->deadline_ms - now;
	status->abi_version = AICPM_L9110S_ABI_VERSION;
	status->direction = core->direction;
	status->last_error = core->last_error;
	status->remaining_ms = min_t(u64, remaining, U32_MAX);
	mutex_unlock(&core->state_lock);
}

int aicpm_l9110s_get_status_transaction(struct aicpm_l9110s_core *core,
					struct aicpm_l9110s_status *status)
{
	int result = 0;

	mutex_lock(&core->op_lock);
	mutex_lock(&core->state_lock);
	if (core->dying)
		result = -ENODEV;
	mutex_unlock(&core->state_lock);
	if (!result)
		aicpm_l9110s_get_status_core(core, status);
	mutex_unlock(&core->op_lock);
	return result;
}

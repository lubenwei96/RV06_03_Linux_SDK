// SPDX-License-Identifier: GPL-2.0
#include <kunit/test.h>

#include <linux/completion.h>
#include <linux/errno.h>
#include <linux/jiffies.h>
#include <linux/kthread.h>
#include <linux/string.h>

#include "aicpm_l9110s_core.h"

enum fake_event {
	FAKE_CANCEL = 1,
	FAKE_STOP,
	FAKE_DEAD_TIME,
	FAKE_DRIVE,
	FAKE_SCHEDULE,
};

struct fake_context {
	int events[64];
	int event_count;
	int cancel_count;
	int stop_count;
	int drive_count;
	int stop_error;
	int fail_stop_call;
	int drive_error;
	u32 last_direction;
	u32 last_duty;
	u32 last_dead_time_us;
	u32 scheduled_ms;
	u64 scheduled_generation;
	u64 now_ms;
	bool block_cancel;
	struct completion cancel_entered;
	struct completion cancel_release;
};

static void fake_event(struct fake_context *fake, int event)
{
	if (fake->event_count < ARRAY_SIZE(fake->events))
		fake->events[fake->event_count++] = event;
}

static int fake_stop(void *context)
{
	struct fake_context *fake = context;

	fake_event(fake, FAKE_STOP);
	fake->stop_count++;
	return fake->stop_count == fake->fail_stop_call ? fake->stop_error : 0;
}

static int fake_drive(void *context, u32 direction, u32 duty_permille)
{
	struct fake_context *fake = context;

	fake_event(fake, FAKE_DRIVE);
	fake->drive_count++;
	fake->last_direction = direction;
	fake->last_duty = duty_permille;
	return fake->drive_error;
}

static void fake_dead_time(void *context, u32 dead_time_us)
{
	struct fake_context *fake = context;

	fake_event(fake, FAKE_DEAD_TIME);
	fake->last_dead_time_us = dead_time_us;
}

static void fake_cancel_timeout_sync(void *context)
{
	struct fake_context *fake = context;

	fake_event(fake, FAKE_CANCEL);
	fake->cancel_count++;
	if (fake->block_cancel) {
		complete(&fake->cancel_entered);
		wait_for_completion(&fake->cancel_release);
	}
}

static void fake_schedule_timeout(void *context, u32 duration_ms, u64 generation)
{
	struct fake_context *fake = context;

	fake_event(fake, FAKE_SCHEDULE);
	fake->scheduled_ms = duration_ms;
	fake->scheduled_generation = generation;
}

static u64 fake_now_ms(void *context)
{
	struct fake_context *fake = context;

	return fake->now_ms;
}

static const struct aicpm_l9110s_core_ops fake_ops = {
	.stop = fake_stop,
	.drive = fake_drive,
	.dead_time = fake_dead_time,
	.cancel_timeout_sync = fake_cancel_timeout_sync,
	.schedule_timeout = fake_schedule_timeout,
	.now_ms = fake_now_ms,
};

static bool fake_wait_completion(struct completion *completion)
{
	return wait_for_completion_timeout(completion,
					   msecs_to_jiffies(5000)) != 0;
}

static void fake_init(struct kunit *test, struct fake_context *fake,
		      struct aicpm_l9110s_core *core)
{
	memset(fake, 0, sizeof(*fake));
	init_completion(&fake->cancel_entered);
	init_completion(&fake->cancel_release);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_core_init(core, &fake_ops, fake,
			2000, 3000), 0);
}

static struct aicpm_l9110s_run fake_run(u32 direction, u32 duration_ms)
{
	struct aicpm_l9110s_run run = {
		.abi_version = AICPM_L9110S_ABI_VERSION,
		.direction = direction,
		.duty_permille = 500,
		.duration_ms = duration_ms,
	};

	return run;
}

static void aicpm_l9110s_rejects_wrong_abi(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run valid = fake_run(AICPM_L9110S_FORWARD, 1000);
	struct aicpm_l9110s_run invalid = valid;
	u64 generation;
	u32 direction;
	int cancel_count;

	fake_init(test, &fake, &core);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_run_transaction(&core, &valid), 0);
	generation = core.generation;
	direction = core.direction;
	cancel_count = fake.cancel_count;
	invalid.abi_version++;
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_run_transaction(&core, &invalid), -EINVAL);
	KUNIT_EXPECT_EQ(test, core.generation, generation);
	KUNIT_EXPECT_EQ(test, core.direction, direction);
	KUNIT_EXPECT_EQ(test, direction, (u32)AICPM_L9110S_FORWARD);
	KUNIT_EXPECT_EQ(test, fake.cancel_count, cancel_count);
	invalid = valid;
	invalid.direction = AICPM_L9110S_STOPPED;
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_run_transaction(&core, &invalid), -EINVAL);
	KUNIT_EXPECT_EQ(test, core.generation, generation);
	KUNIT_EXPECT_EQ(test, core.direction, direction);
	KUNIT_EXPECT_EQ(test, fake.cancel_count, cancel_count);
	invalid = valid;
	invalid.duty_permille = 0;
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_run_transaction(&core, &invalid), -EINVAL);
	KUNIT_EXPECT_EQ(test, core.generation, generation);
	KUNIT_EXPECT_EQ(test, core.direction, direction);
	KUNIT_EXPECT_EQ(test, fake.cancel_count, cancel_count);
	invalid = valid;
	invalid.duty_permille = 1001;
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_run_transaction(&core, &invalid), -EINVAL);
	KUNIT_EXPECT_EQ(test, core.generation, generation);
	KUNIT_EXPECT_EQ(test, core.direction, direction);
	KUNIT_EXPECT_EQ(test, fake.cancel_count, cancel_count);
}

static void aicpm_l9110s_rejects_zero_duration(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run run = fake_run(AICPM_L9110S_FORWARD, 0);

	fake_init(test, &fake, &core);
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_run_transaction(&core, &run), -EINVAL);
	KUNIT_EXPECT_EQ(test, fake.cancel_count, 0);
	KUNIT_EXPECT_EQ(test, fake.stop_count, 0);
}

static void aicpm_l9110s_rejects_duration_above_dt_limit(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct aicpm_l9110s_core invalid_core;
	struct fake_context fake;
	struct aicpm_l9110s_run run = fake_run(AICPM_L9110S_FORWARD, 3001);

	fake_init(test, &fake, &core);
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_run_transaction(&core, &run), -ERANGE);
	KUNIT_EXPECT_EQ(test, fake.event_count, 0);
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_core_init(&invalid_core, &fake_ops,
			&fake, 100001, 3000), -ERANGE);
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_core_init(&invalid_core, &fake_ops,
			&fake, 2000, 30001), -ERANGE);
}

static void aicpm_l9110s_orders_drive_and_status_counts_down(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run run = fake_run(AICPM_L9110S_FORWARD, 1000);
	struct aicpm_l9110s_status status;
	int expected[] = { FAKE_CANCEL, FAKE_STOP, FAKE_DEAD_TIME,
			   FAKE_DRIVE, FAKE_SCHEDULE };
	int index;

	fake_init(test, &fake, &core);
	fake.now_ms = 100;
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_run_transaction(&core, &run), 0);
	for (index = 0; index < ARRAY_SIZE(expected); index++)
		KUNIT_EXPECT_EQ(test, fake.events[index], expected[index]);
	KUNIT_EXPECT_EQ(test, fake.last_dead_time_us, 2000U);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_get_status_transaction(&core, &status), 0);
	KUNIT_EXPECT_EQ(test, status.remaining_ms, 1000U);
	fake.now_ms = 500;
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_get_status_transaction(&core, &status), 0);
	KUNIT_EXPECT_EQ(test, status.remaining_ms, 600U);
	fake.now_ms = 1200;
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_get_status_transaction(&core, &status), 0);
	KUNIT_EXPECT_EQ(test, status.remaining_ms, 0U);
}

static void aicpm_l9110s_rejects_second_open(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct fake_context fake;

	fake_init(test, &fake, &core);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_open_transaction(&core), 0);
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_open_transaction(&core), -EBUSY);
}

static void aicpm_l9110s_close_forces_stop(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run run = fake_run(AICPM_L9110S_FORWARD, 1000);
	struct aicpm_l9110s_status status;

	fake_init(test, &fake, &core);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_open_transaction(&core), 0);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_run_transaction(&core, &run), 0);
	aicpm_l9110s_close_transaction(&core);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_get_status_transaction(&core, &status), 0);
	KUNIT_EXPECT_EQ(test, status.direction, (u32)AICPM_L9110S_STOPPED);
	KUNIT_EXPECT_EQ(test, status.remaining_ms, 0U);
	KUNIT_EXPECT_EQ(test, status.last_error, -ECANCELED);
}

static void aicpm_l9110s_timeout_forces_stop_without_self_cancel(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run run = fake_run(AICPM_L9110S_FORWARD, 1000);
	struct aicpm_l9110s_status status;
	u64 generation;
	int cancel_count;

	fake_init(test, &fake, &core);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_run_transaction(&core, &run), 0);
	generation = fake.scheduled_generation;
	cancel_count = fake.cancel_count;
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_timeout_core(&core, generation), 0);
	KUNIT_EXPECT_EQ(test, fake.cancel_count, cancel_count);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_get_status_transaction(&core, &status), 0);
	KUNIT_EXPECT_EQ(test, status.direction, (u32)AICPM_L9110S_STOPPED);
	KUNIT_EXPECT_EQ(test, status.remaining_ms, 0U);
	KUNIT_EXPECT_EQ(test, status.last_error, -ETIMEDOUT);
}

struct thread_call {
	struct aicpm_l9110s_core *core;
	struct aicpm_l9110s_run run;
	u64 generation;
	int result;
	struct completion started;
	struct completion done;
};

static int remove_thread(void *data)
{
	struct thread_call *call = data;

	complete(&call->started);
	call->result = aicpm_l9110s_begin_remove_transaction(call->core, -ENODEV);
	complete(&call->done);
	return 0;
}

static int status_thread(void *data)
{
	struct thread_call *call = data;
	struct aicpm_l9110s_status status;

	complete(&call->started);
	call->result = aicpm_l9110s_get_status_transaction(call->core, &status);
	complete(&call->done);
	return 0;
}

static int run_thread(void *data)
{
	struct thread_call *call = data;

	complete(&call->started);
	call->result = aicpm_l9110s_run_transaction(call->core, &call->run);
	complete(&call->done);
	return 0;
}

static int stop_thread(void *data)
{
	struct thread_call *call = data;

	complete(&call->started);
	call->result = aicpm_l9110s_stop_transaction(call->core, 0);
	complete(&call->done);
	return 0;
}

static void aicpm_l9110s_run_stop_race_leaves_outputs_low(struct kunit *test)
{
	struct aicpm_l9110s_core race_core;
	struct fake_context race_fake;
	struct aicpm_l9110s_run first_run =
		fake_run(AICPM_L9110S_FORWARD, 1000);
	struct thread_call concurrent_run = {
		.core = &race_core,
		.run = {
			.abi_version = AICPM_L9110S_ABI_VERSION,
			.direction = AICPM_L9110S_REVERSE,
			.duty_permille = 600,
			.duration_ms = 500,
		},
	};
	struct thread_call concurrent_stop = { .core = &race_core };
	struct aicpm_l9110s_status race_status;
	struct task_struct *run_task;
	struct task_struct *stop_task;
	int expected[] = { FAKE_CANCEL, FAKE_STOP, FAKE_DEAD_TIME,
			   FAKE_DRIVE, FAKE_SCHEDULE, FAKE_CANCEL, FAKE_STOP };
	int event_base;
	int index;
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run remove_run =
		fake_run(AICPM_L9110S_FORWARD, 1000);
	struct thread_call begin_remove = { .core = &core };
	struct thread_call existing_fd_ioctl = { .core = &core };
	struct task_struct *remove_task;
	struct task_struct *ioctl_task;
	int stop_count;
	int cancel_count;

	/* A blocked RUN owns op_lock, so STOP must serialize after it. */
	fake_init(test, &race_fake, &race_core);
	init_completion(&concurrent_run.started);
	init_completion(&concurrent_run.done);
	init_completion(&concurrent_stop.started);
	init_completion(&concurrent_stop.done);
	KUNIT_ASSERT_EQ(test,
			aicpm_l9110s_run_transaction(&race_core, &first_run), 0);
	event_base = race_fake.event_count;
	race_fake.block_cancel = true;
	run_task = kthread_run(run_thread, &concurrent_run, "aicpm-run-race-test");
	KUNIT_ASSERT_FALSE(test, IS_ERR(run_task));
	if (!fake_wait_completion(&race_fake.cancel_entered)) {
		race_fake.block_cancel = false;
		complete_all(&race_fake.cancel_release);
		kthread_stop(run_task);
		KUNIT_FAIL(test, "RUN did not enter cancel before timeout");
		return;
	}
	stop_task = kthread_run(stop_thread, &concurrent_stop,
				"aicpm-stop-race-test");
	if (IS_ERR(stop_task)) {
		race_fake.block_cancel = false;
		complete_all(&race_fake.cancel_release);
		kthread_stop(run_task);
		KUNIT_FAIL(test, "failed to create STOP thread: %ld",
			   PTR_ERR(stop_task));
		return;
	}
	if (!fake_wait_completion(&concurrent_stop.started)) {
		race_fake.block_cancel = false;
		complete_all(&race_fake.cancel_release);
		kthread_stop(stop_task);
		kthread_stop(run_task);
		KUNIT_FAIL(test, "STOP thread did not start before timeout");
		return;
	}
	KUNIT_EXPECT_FALSE(test, completion_done(&concurrent_stop.done));
	race_fake.block_cancel = false;
	complete_all(&race_fake.cancel_release);
	if (!fake_wait_completion(&concurrent_run.done) ||
	    !fake_wait_completion(&concurrent_stop.done)) {
		kthread_stop(stop_task);
		kthread_stop(run_task);
		KUNIT_FAIL(test, "RUN/STOP threads did not serialize before timeout");
		return;
	}
	KUNIT_EXPECT_EQ(test, kthread_stop(stop_task), 0);
	KUNIT_EXPECT_EQ(test, kthread_stop(run_task), 0);
	KUNIT_EXPECT_EQ(test, concurrent_run.result, 0);
	KUNIT_EXPECT_EQ(test, concurrent_stop.result, 0);
	for (index = 0; index < ARRAY_SIZE(expected); index++)
		KUNIT_EXPECT_EQ(test, race_fake.events[event_base + index],
				expected[index]);
	KUNIT_EXPECT_EQ(test, race_fake.stop_count, 3);
	KUNIT_ASSERT_EQ(test,
			aicpm_l9110s_get_status_transaction(&race_core,
						       &race_status), 0);
	KUNIT_EXPECT_EQ(test, race_status.direction,
			(u32)AICPM_L9110S_STOPPED);
	KUNIT_EXPECT_EQ(test, race_status.remaining_ms, 0U);
	KUNIT_EXPECT_EQ(test, race_status.last_error, 0);

	/* Retain the remove/existing-fd lifetime subscenario in this case. */
	fake_init(test, &fake, &core);
	init_completion(&begin_remove.started);
	init_completion(&begin_remove.done);
	init_completion(&existing_fd_ioctl.started);
	init_completion(&existing_fd_ioctl.done);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_open_transaction(&core), 0);
	KUNIT_ASSERT_EQ(test,
			aicpm_l9110s_run_transaction(&core, &remove_run), 0);
	fake.block_cancel = true;
	remove_task = kthread_run(remove_thread, &begin_remove, "aicpm-remove-test");
	KUNIT_ASSERT_FALSE(test, IS_ERR(remove_task));
	if (!fake_wait_completion(&fake.cancel_entered)) {
		fake.block_cancel = false;
		complete_all(&fake.cancel_release);
		kthread_stop(remove_task);
		KUNIT_FAIL(test, "remove did not enter cancel before timeout");
		return;
	}
	ioctl_task = kthread_run(status_thread, &existing_fd_ioctl, "aicpm-ioctl-test");
	if (IS_ERR(ioctl_task)) {
		fake.block_cancel = false;
		complete_all(&fake.cancel_release);
		kthread_stop(remove_task);
		KUNIT_FAIL(test, "failed to create existing-fd ioctl thread: %ld",
			   PTR_ERR(ioctl_task));
		return;
	}
	if (!fake_wait_completion(&existing_fd_ioctl.started)) {
		fake.block_cancel = false;
		complete_all(&fake.cancel_release);
		kthread_stop(ioctl_task);
		kthread_stop(remove_task);
		KUNIT_FAIL(test, "existing-fd ioctl did not start before timeout");
		return;
	}
	KUNIT_EXPECT_FALSE(test, completion_done(&existing_fd_ioctl.done));
	fake.block_cancel = false;
	complete_all(&fake.cancel_release);
	if (!fake_wait_completion(&begin_remove.done) ||
	    !fake_wait_completion(&existing_fd_ioctl.done)) {
		kthread_stop(ioctl_task);
		kthread_stop(remove_task);
		KUNIT_FAIL(test, "remove/ioctl threads did not finish before timeout");
		return;
	}
	KUNIT_EXPECT_EQ(test, kthread_stop(ioctl_task), 0);
	KUNIT_EXPECT_EQ(test, kthread_stop(remove_task), 0);
	KUNIT_EXPECT_EQ(test, begin_remove.result, 0);
	KUNIT_EXPECT_EQ(test, existing_fd_ioctl.result, -ENODEV);
	stop_count = fake.stop_count;
	cancel_count = fake.cancel_count;
	aicpm_l9110s_close_transaction(&core);
	KUNIT_EXPECT_EQ(test, fake.stop_count, stop_count);
	KUNIT_EXPECT_EQ(test, fake.cancel_count, cancel_count);
}

static int timeout_thread(void *data)
{
	struct thread_call *call = data;

	complete(&call->started);
	call->result = aicpm_l9110s_timeout_core(call->core, call->generation);
	complete(&call->done);
	return 0;
}

static void aicpm_l9110s_concurrent_runs_keep_newest_timeout(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run old_run = fake_run(AICPM_L9110S_FORWARD, 1000);
	struct thread_call new_run = {
		.core = &core,
		.run = {
			.abi_version = AICPM_L9110S_ABI_VERSION,
			.direction = AICPM_L9110S_REVERSE,
			.duty_permille = 600,
			.duration_ms = 100,
		},
	};
	struct thread_call old_worker = { .core = &core };
	struct aicpm_l9110s_status status;
	struct task_struct *run_task;
	struct task_struct *worker_task;
	u64 old_generation;

	fake_init(test, &fake, &core);
	init_completion(&new_run.started);
	init_completion(&new_run.done);
	init_completion(&old_worker.started);
	init_completion(&old_worker.done);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_run_transaction(&core, &old_run), 0);
	old_generation = fake.scheduled_generation;
	old_worker.generation = old_generation;
	fake.block_cancel = true;
	run_task = kthread_run(run_thread, &new_run, "aicpm-new-run-test");
	KUNIT_ASSERT_FALSE(test, IS_ERR(run_task));
	wait_for_completion(&fake.cancel_entered);
	worker_task = kthread_run(timeout_thread, &old_worker, "aicpm-old-timeout-test");
	if (IS_ERR(worker_task)) {
		fake.block_cancel = false;
		complete_all(&fake.cancel_release);
		kthread_stop(run_task);
		KUNIT_FAIL(test, "failed to create old-worker thread: %ld",
			   PTR_ERR(worker_task));
		return;
	}
	wait_for_completion(&old_worker.done);
	fake.block_cancel = false;
	complete_all(&fake.cancel_release);
	wait_for_completion(&new_run.done);
	KUNIT_EXPECT_EQ(test, kthread_stop(worker_task), 0);
	KUNIT_EXPECT_EQ(test, kthread_stop(run_task), 0);
	KUNIT_ASSERT_EQ(test, new_run.result, 0);
	KUNIT_ASSERT_EQ(test, old_worker.result, 0);
	KUNIT_EXPECT_GT(test, fake.scheduled_generation, old_generation);
	KUNIT_EXPECT_EQ(test, fake.scheduled_ms, 100U);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_timeout_core(&core, old_generation), 0);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_get_status_transaction(&core, &status), 0);
	KUNIT_EXPECT_EQ(test, status.direction, (u32)AICPM_L9110S_REVERSE);
	KUNIT_EXPECT_EQ(test, status.remaining_ms, 100U);
}

static void aicpm_l9110s_suspend_forces_stop(struct kunit *test)
{
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run run = fake_run(AICPM_L9110S_FORWARD, 1000);
	struct aicpm_l9110s_status status;

	fake_init(test, &fake, &core);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_run_transaction(&core, &run), 0);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_stop_transaction(&core, -EHOSTDOWN), 0);
	KUNIT_ASSERT_EQ(test, aicpm_l9110s_get_status_transaction(&core, &status), 0);
	KUNIT_EXPECT_EQ(test, status.direction, (u32)AICPM_L9110S_STOPPED);
	KUNIT_EXPECT_EQ(test, status.remaining_ms, 0U);
	KUNIT_EXPECT_EQ(test, status.last_error, -EHOSTDOWN);
}

struct fake_pair_context {
	int fail_apply_call;
	int fail_apply_call2;
	int fail_apply_call3;
	int fail_errno;
	int fail_errno2;
	int fail_errno3;
	int call_count;
	enum aicpm_l9110s_output outputs[8];
	u32 duties[8];
};

static int fake_pair_apply(void *context, enum aicpm_l9110s_output output,
			   u32 duty_permille)
{
	struct fake_pair_context *fake = context;
	int call = ++fake->call_count;

	fake->outputs[call - 1] = output;
	fake->duties[call - 1] = duty_permille;
	if (call == fake->fail_apply_call)
		return fake->fail_errno;
	if (call == fake->fail_apply_call2)
		return fake->fail_errno2;
	if (call == fake->fail_apply_call3)
		return fake->fail_errno3;
	return 0;
}

static const struct aicpm_l9110s_pair_ops fake_pair_ops = {
	.apply = fake_pair_apply,
};

static void aicpm_l9110s_hardware_error_forces_stop(struct kunit *test)
{
	struct aicpm_l9110s_pair pair;
	struct fake_pair_context pair_fake = { 0 };
	struct aicpm_l9110s_core core;
	struct fake_context fake;
	struct aicpm_l9110s_run run = fake_run(AICPM_L9110S_FORWARD, 1000);
	struct aicpm_l9110s_status status;
	int first_error;
	int drive_count;

	aicpm_l9110s_pair_init(&pair, &fake_pair_ops, &pair_fake);
	pair.active_output = AICPM_L9110S_OUTPUT_B;
	pair_fake.fail_apply_call = 1;
	pair_fake.fail_errno = -EIO;
	pair_fake.fail_apply_call2 = 3;
	pair_fake.fail_errno2 = -EAGAIN;
	pair_fake.fail_apply_call3 = 4;
	pair_fake.fail_errno3 = -ENOSPC;
	first_error = aicpm_l9110s_pair_stop(&pair);
	KUNIT_EXPECT_EQ(test, first_error, -EIO);
	KUNIT_EXPECT_EQ(test, pair_fake.call_count, 2);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[0],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[1],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_A);
	KUNIT_EXPECT_EQ(test, pair.active_output,
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	first_error = aicpm_l9110s_pair_stop(&pair);
	KUNIT_EXPECT_EQ(test, first_error, -EAGAIN);
	KUNIT_EXPECT_EQ(test, pair_fake.call_count, 4);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[2],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[3],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_A);
	KUNIT_EXPECT_EQ(test, pair.active_output,
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_pair_stop(&pair), 0);
	KUNIT_EXPECT_EQ(test, pair_fake.call_count, 6);
	KUNIT_EXPECT_EQ(test, pair.active_output,
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_NONE);

	memset(&pair_fake, 0, sizeof(pair_fake));
	aicpm_l9110s_pair_init(&pair, &fake_pair_ops, &pair_fake);
	pair_fake.fail_apply_call = 1;
	pair_fake.fail_errno = -EREMOTEIO;
	first_error = aicpm_l9110s_pair_drive(&pair,
					 AICPM_L9110S_FORWARD, 500);
	KUNIT_EXPECT_EQ(test, first_error, -EREMOTEIO);
	KUNIT_EXPECT_EQ(test, pair_fake.call_count, 3);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[0],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[1],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[2],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_A);

	memset(&pair_fake, 0, sizeof(pair_fake));
	aicpm_l9110s_pair_init(&pair, &fake_pair_ops, &pair_fake);
	pair_fake.fail_apply_call = 2;
	pair_fake.fail_errno = -EPIPE;
	pair_fake.fail_apply_call2 = 3;
	pair_fake.fail_errno2 = -EBUSY;
	pair_fake.fail_apply_call3 = 4;
	pair_fake.fail_errno3 = -ENOSPC;
	first_error = aicpm_l9110s_pair_drive(&pair, AICPM_L9110S_FORWARD, 500);
	KUNIT_EXPECT_EQ(test, first_error, -EPIPE);
	KUNIT_EXPECT_EQ(test, pair_fake.call_count, 4);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[0],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	KUNIT_EXPECT_EQ(test, pair_fake.duties[0], 0U);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[1],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_A);
	KUNIT_EXPECT_EQ(test, pair_fake.duties[1], 500U);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[2],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_A);
	KUNIT_EXPECT_EQ(test, pair_fake.duties[2], 0U);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[3],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	KUNIT_EXPECT_EQ(test, pair_fake.duties[3], 0U);
	KUNIT_EXPECT_EQ(test, pair.active_output,
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_A);
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_pair_stop(&pair), 0);
	KUNIT_EXPECT_EQ(test, pair_fake.call_count, 6);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[4],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_A);
	KUNIT_EXPECT_EQ(test, pair_fake.outputs[5],
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_B);
	KUNIT_EXPECT_EQ(test, pair.active_output,
			(enum aicpm_l9110s_output)AICPM_L9110S_OUTPUT_NONE);

	fake_init(test, &fake, &core);
	fake.drive_error = -EIO;
	fake.stop_error = -EBUSY;
	fake.fail_stop_call = 2;
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_run_transaction(&core, &run), -EIO);
	KUNIT_EXPECT_TRUE(test, core.fault_latched);
	KUNIT_EXPECT_EQ(test, core.fault_errno, -EIO);
	drive_count = fake.drive_count;
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_run_transaction(&core, &run), -EIO);
	KUNIT_EXPECT_EQ(test, fake.drive_count, drive_count);
	first_error = fake.stop_count;
	KUNIT_EXPECT_EQ(test, aicpm_l9110s_stop_transaction(&core, 0), -EIO);
	KUNIT_EXPECT_GT(test, fake.stop_count, first_error);
	KUNIT_EXPECT_TRUE(test, core.fault_latched);
	KUNIT_EXPECT_EQ(test, core.fault_errno, -EIO);
	aicpm_l9110s_get_status_core(&core, &status);
	KUNIT_EXPECT_EQ(test, status.direction, (u32)AICPM_L9110S_STOPPED);
	KUNIT_EXPECT_EQ(test, status.remaining_ms, 0U);
	KUNIT_EXPECT_EQ(test, status.last_error, -EIO);
}

static struct kunit_case aicpm_l9110s_test_cases[] = {
	KUNIT_CASE(aicpm_l9110s_rejects_wrong_abi),
	KUNIT_CASE(aicpm_l9110s_rejects_zero_duration),
	KUNIT_CASE(aicpm_l9110s_rejects_duration_above_dt_limit),
	KUNIT_CASE(aicpm_l9110s_orders_drive_and_status_counts_down),
	KUNIT_CASE(aicpm_l9110s_rejects_second_open),
	KUNIT_CASE(aicpm_l9110s_close_forces_stop),
	KUNIT_CASE(aicpm_l9110s_timeout_forces_stop_without_self_cancel),
	KUNIT_CASE(aicpm_l9110s_run_stop_race_leaves_outputs_low),
	KUNIT_CASE(aicpm_l9110s_concurrent_runs_keep_newest_timeout),
	KUNIT_CASE(aicpm_l9110s_suspend_forces_stop),
	KUNIT_CASE(aicpm_l9110s_hardware_error_forces_stop),
	{}
};

static struct kunit_suite aicpm_l9110s_test_suite = {
	.name = "aicpm_l9110s",
	.test_cases = aicpm_l9110s_test_cases,
};

kunit_test_suite(aicpm_l9110s_test_suite);

// SPDX-License-Identifier: GPL-2.0
#include <linux/build_bug.h>
#include <linux/compat.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/kref.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/pm.h>
#include <linux/pwm.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/workqueue.h>

#include "aicpm_l9110s_core.h"

static_assert(sizeof(struct aicpm_l9110s_run) == 16);
static_assert(sizeof(struct aicpm_l9110s_status) == 16);

struct aicpm_l9110s_motor {
	struct kref refcount;
	struct miscdevice miscdev;
	struct pwm_device *pwms[2];
	struct delayed_work timeout_work;
	struct aicpm_l9110s_pair pair;
	struct aicpm_l9110s_core core;
	u64 timeout_generation;
};

static struct pwm_device *aicpm_l9110s_pwm(struct aicpm_l9110s_motor *motor,
					  enum aicpm_l9110s_output output)
{
	return motor->pwms[output == AICPM_L9110S_OUTPUT_A ? 0 : 1];
}

static int aicpm_l9110s_apply_output(struct aicpm_l9110s_motor *motor,
				     enum aicpm_l9110s_output output,
				     u32 duty_permille)
{
	struct pwm_device *pwm = aicpm_l9110s_pwm(motor, output);
	struct pwm_state state;
	int result;

	pwm_init_state(pwm, &state);
	if (!state.period)
		return -EINVAL;
	state.polarity = PWM_POLARITY_NORMAL;
	state.enabled = true;
	result = pwm_set_relative_duty_cycle(&state, duty_permille, 1000);
	if (result)
		return result;
	return pwm_apply_state(pwm, &state);
}

static int aicpm_l9110s_pair_apply(void *context,
				   enum aicpm_l9110s_output output,
				   u32 duty_permille)
{
	return aicpm_l9110s_apply_output(context, output, duty_permille);
}

static const struct aicpm_l9110s_pair_ops aicpm_l9110s_pair_ops = {
	.apply = aicpm_l9110s_pair_apply,
};

void aicpm_l9110s_pair_init(struct aicpm_l9110s_pair *pair,
			    const struct aicpm_l9110s_pair_ops *ops,
			    void *context)
{
	pair->ops = ops;
	pair->context = context;
	pair->active_output = AICPM_L9110S_OUTPUT_NONE;
}

int aicpm_l9110s_pair_stop(struct aicpm_l9110s_pair *pair)
{
	enum aicpm_l9110s_output first;
	enum aicpm_l9110s_output second;
	int first_error;
	int result;

	first = pair->active_output;
	if (first == AICPM_L9110S_OUTPUT_NONE)
		first = AICPM_L9110S_OUTPUT_A;
	second = first == AICPM_L9110S_OUTPUT_A ?
		 AICPM_L9110S_OUTPUT_B : AICPM_L9110S_OUTPUT_A;
	first_error = pair->ops->apply(pair->context, first, 0);
	result = pair->ops->apply(pair->context, second, 0);
	if (!first_error && !result)
		pair->active_output = AICPM_L9110S_OUTPUT_NONE;
	else if (first_error)
		pair->active_output = first;
	else
		pair->active_output = second;
	if (!first_error)
		first_error = result;
	return first_error;
}

int aicpm_l9110s_pair_drive(struct aicpm_l9110s_pair *pair,
			    u32 direction, u32 duty_permille)
{
	enum aicpm_l9110s_output active;
	enum aicpm_l9110s_output inactive;
	int cleanup_error;
	int first_error;

	active = direction == AICPM_L9110S_FORWARD ?
		 AICPM_L9110S_OUTPUT_A : AICPM_L9110S_OUTPUT_B;
	inactive = active == AICPM_L9110S_OUTPUT_A ?
		   AICPM_L9110S_OUTPUT_B : AICPM_L9110S_OUTPUT_A;
	first_error = pair->ops->apply(pair->context, inactive, 0);
	if (first_error) {
		pair->active_output = inactive;
		goto cleanup;
	}
	pair->active_output = active;
	first_error = pair->ops->apply(pair->context, active, duty_permille);
	if (!first_error)
		return 0;
cleanup:
	cleanup_error = aicpm_l9110s_pair_stop(pair);
	(void)cleanup_error;
	return first_error;
}

static int aicpm_l9110s_stop(void *context)
{
	struct aicpm_l9110s_motor *motor = context;

	return aicpm_l9110s_pair_stop(&motor->pair);
}

static int aicpm_l9110s_drive(void *context, u32 direction,
			      u32 duty_permille)
{
	struct aicpm_l9110s_motor *motor = context;

	return aicpm_l9110s_pair_drive(&motor->pair, direction, duty_permille);
}

static void aicpm_l9110s_dead_time(void *context, u32 dead_time_us)
{
	(void)context;
	usleep_range(dead_time_us, dead_time_us + max_t(u32, 100, dead_time_us / 10));
}

static void aicpm_l9110s_cancel_timeout_sync(void *context)
{
	struct aicpm_l9110s_motor *motor = context;

	(void)cancel_delayed_work_sync(&motor->timeout_work);
}

static void aicpm_l9110s_schedule_timeout(void *context, u32 duration_ms,
					  u64 generation)
{
	struct aicpm_l9110s_motor *motor = context;

	mutex_lock(&motor->core.state_lock);
	motor->timeout_generation = generation;
	mutex_unlock(&motor->core.state_lock);
	(void)mod_delayed_work(system_wq, &motor->timeout_work,
			       msecs_to_jiffies(duration_ms));
}

static u64 aicpm_l9110s_now_ms(void *context)
{
	(void)context;
	return ktime_to_ms(ktime_get());
}

static const struct aicpm_l9110s_core_ops aicpm_l9110s_core_ops = {
	.stop = aicpm_l9110s_stop,
	.drive = aicpm_l9110s_drive,
	.dead_time = aicpm_l9110s_dead_time,
	.cancel_timeout_sync = aicpm_l9110s_cancel_timeout_sync,
	.schedule_timeout = aicpm_l9110s_schedule_timeout,
	.now_ms = aicpm_l9110s_now_ms,
};

static void aicpm_l9110s_timeout_work(struct work_struct *work)
{
	struct aicpm_l9110s_motor *motor = container_of(to_delayed_work(work),
						      struct aicpm_l9110s_motor,
						      timeout_work);
	u64 generation;

	mutex_lock(&motor->core.state_lock);
	generation = motor->timeout_generation;
	mutex_unlock(&motor->core.state_lock);
	(void)aicpm_l9110s_timeout_core(&motor->core, generation);
}

static void aicpm_l9110s_free(struct kref *refcount)
{
	struct aicpm_l9110s_motor *motor = container_of(refcount,
						      struct aicpm_l9110s_motor,
						      refcount);

	kfree(motor);
}

static int aicpm_l9110s_open(struct inode *inode, struct file *file)
{
	struct miscdevice *miscdev = file->private_data;
	struct aicpm_l9110s_motor *motor = container_of(miscdev,
						      struct aicpm_l9110s_motor,
						      miscdev);
	int ret;

	(void)inode;
	ret = aicpm_l9110s_open_transaction(&motor->core);
	if (ret)
		return ret;
	kref_get(&motor->refcount);
	file->private_data = motor;
	return 0;
}

static int aicpm_l9110s_release(struct inode *inode, struct file *file)
{
	struct aicpm_l9110s_motor *motor = file->private_data;

	(void)inode;
	aicpm_l9110s_close_transaction(&motor->core);
	kref_put(&motor->refcount, aicpm_l9110s_free);
	return 0;
}

static long aicpm_l9110s_ioctl(struct file *file, unsigned int command,
			       unsigned long argument)
{
	struct aicpm_l9110s_motor *motor = file->private_data;
	void __user *user = (void __user *)argument;
	struct aicpm_l9110s_run run;
	struct aicpm_l9110s_status status = { 0 };
	int result;

	switch (command) {
	case AICPM_L9110S_IOC_RUN:
	case AICPM_L9110S_IOC_STOP:
	case AICPM_L9110S_IOC_GET_STATUS:
		break;
	default:
		return -ENOTTY;
	}
	result = aicpm_l9110s_check_live_transaction(&motor->core);
	if (result)
		return result;

	switch (command) {
	case AICPM_L9110S_IOC_RUN:
		if (copy_from_user(&run, user, sizeof(run)))
			return -EFAULT;
		return aicpm_l9110s_run_transaction(&motor->core, &run);
	case AICPM_L9110S_IOC_STOP:
		return aicpm_l9110s_stop_transaction(&motor->core, 0);
	case AICPM_L9110S_IOC_GET_STATUS:
		result = aicpm_l9110s_get_status_transaction(&motor->core, &status);
		if (result)
			return result;
		if (copy_to_user(user, &status, sizeof(status)))
			return -EFAULT;
		return 0;
	default:
		return -ENOTTY;
	}
}

static const struct file_operations aicpm_l9110s_fops = {
	.owner = THIS_MODULE,
	.open = aicpm_l9110s_open,
	.release = aicpm_l9110s_release,
	.unlocked_ioctl = aicpm_l9110s_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl = compat_ptr_ioctl,
#endif
	.llseek = no_llseek,
};

static int aicpm_l9110s_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct aicpm_l9110s_motor *motor;
	u32 dead_time_us;
	u32 max_run_time_ms;
	int result;

	motor = kzalloc(sizeof(*motor), GFP_KERNEL);
	if (!motor)
		return -ENOMEM;
	kref_init(&motor->refcount);
	motor->pwms[0] = devm_pwm_get(dev, "in-a");
	if (IS_ERR(motor->pwms[0])) {
		result = PTR_ERR(motor->pwms[0]);
		goto put_motor;
	}
	motor->pwms[1] = devm_pwm_get(dev, "in-b");
	if (IS_ERR(motor->pwms[1])) {
		result = PTR_ERR(motor->pwms[1]);
		goto put_motor;
	}
	result = of_property_read_u32(dev->of_node, "aicpm,dead-time-us",
				      &dead_time_us);
	if (result)
		goto put_motor;
	result = of_property_read_u32(dev->of_node, "aicpm,max-run-time-ms",
				      &max_run_time_ms);
	if (result)
		goto put_motor;

	INIT_DELAYED_WORK(&motor->timeout_work, aicpm_l9110s_timeout_work);
	aicpm_l9110s_pair_init(&motor->pair, &aicpm_l9110s_pair_ops, motor);
	result = aicpm_l9110s_core_init(&motor->core, &aicpm_l9110s_core_ops,
					motor, dead_time_us, max_run_time_ms);
	if (result)
		goto put_motor;
	motor->miscdev.minor = MISC_DYNAMIC_MINOR;
	motor->miscdev.name = "aicpm-l9110s0";
	motor->miscdev.fops = &aicpm_l9110s_fops;
	motor->miscdev.parent = dev;
	platform_set_drvdata(pdev, motor);
	result = aicpm_l9110s_stop_transaction(&motor->core, 0);
	if (result)
		goto clear_drvdata;
	result = misc_register(&motor->miscdev);
	if (result)
		goto clear_drvdata;
	return 0;

clear_drvdata:
	platform_set_drvdata(pdev, NULL);
put_motor:
	kref_put(&motor->refcount, aicpm_l9110s_free);
	return result;
}

static int aicpm_l9110s_remove(struct platform_device *pdev)
{
	struct aicpm_l9110s_motor *motor = platform_get_drvdata(pdev);

	misc_deregister(&motor->miscdev);
	(void)aicpm_l9110s_begin_remove_transaction(&motor->core, -ENODEV);
	platform_set_drvdata(pdev, NULL);
	kref_put(&motor->refcount, aicpm_l9110s_free);
	return 0;
}

static void aicpm_l9110s_shutdown(struct platform_device *pdev)
{
	struct aicpm_l9110s_motor *motor = platform_get_drvdata(pdev);

	if (motor)
		(void)aicpm_l9110s_begin_remove_transaction(&motor->core, -ESHUTDOWN);
}

static int aicpm_l9110s_suspend(struct device *dev)
{
	struct aicpm_l9110s_motor *motor = dev_get_drvdata(dev);

	return aicpm_l9110s_stop_transaction(&motor->core, -EHOSTDOWN);
}

static int aicpm_l9110s_resume(struct device *dev)
{
	(void)dev;
	return 0;
}

static const struct dev_pm_ops aicpm_l9110s_pm_ops = {
	SET_SYSTEM_SLEEP_PM_OPS(aicpm_l9110s_suspend, aicpm_l9110s_resume)
};

static const struct of_device_id aicpm_l9110s_of_match[] = {
	{ .compatible = "aicpm,l9110s" },
	{ }
};
MODULE_DEVICE_TABLE(of, aicpm_l9110s_of_match);

static struct platform_driver aicpm_l9110s_driver = {
	.probe = aicpm_l9110s_probe,
	.remove = aicpm_l9110s_remove,
	.shutdown = aicpm_l9110s_shutdown,
	.driver = {
		.name = "aicpm-l9110s",
		.of_match_table = aicpm_l9110s_of_match,
		.pm = &aicpm_l9110s_pm_ops,
	},
};
module_platform_driver(aicpm_l9110s_driver);

MODULE_DESCRIPTION("AICPM fail-safe timed L9110S motor controller");
MODULE_LICENSE("GPL");

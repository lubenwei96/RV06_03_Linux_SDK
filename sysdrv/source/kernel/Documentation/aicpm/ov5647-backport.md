# OV5647 RAW10 backport for AICPM V1

## Provenance

The mode tables and baseline driver logic are backported from Linux v6.1:

- URL: `https://raw.githubusercontent.com/torvalds/linux/v6.1/drivers/media/i2c/ov5647.c`
- SHA-256: `b3cd7295dd3bdd551357a0812a04778d54272955e8fa2ed3a6a4a17c0eebbb64`

Only the test-pattern register, menu, and values are taken from Linux v6.12:

- URL: `https://raw.githubusercontent.com/torvalds/linux/v6.12/drivers/media/i2c/ov5647.c`
- SHA-256: `c99a6c3b9c60b7c1655c690cc5acc4a13e20a222fe840386efc99545c45c1873`
- Register `0x503d`: Disabled `0x00`, Color Bars `0x80`, Color Squares
  `0x82`, and Random Data `0x81`.

No content was taken from the upstream master branch.

## Modes

| Output | Pixel rate | HTS | VTS | Link frequency | Nominal interval |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2592x1944 RAW10 | 87,500,000 | 2844 | `0x7b0` | 218,750,000 Hz | 1/15 s |
| 1920x1080 RAW10 | 81,666,700 | 2416 | `0x450` | 204,166,750 Hz | 1/30 s |
| 1296x972 RAW10 | 81,666,700 | 1896 | `0x59b` | 204,166,750 Hz | 1/30 s |
| 640x480 RAW10 | 55,000,000 | 1852 | `0x1f8` | 137,500,000 Hz | 1/60 s |

The link frequencies use the fixed two-lane RAW10 calculation
`pixel_rate * 10 / (2 * 2)` and are not module sales-page values.

## RV1106 SDK adaptations

- The v6.1 state-based pad callbacks are adapted to the SDK's Linux 5.10
  `struct v4l2_subdev_pad_config` API. Frame-interval enumeration and media-bus
  configuration expose the fixed mode interval and two-lane CSI-2 clock mode.
- Rockchip `RKMODULE_GET_MODULE_INFO`, `RKMODULE_GET_HDR_CFG` (linear `NO_HDR`),
  and `RKMODULE_GET_CHANNEL_INFO` ioctls are implemented, including the
  `CONFIG_COMPAT` copy-to-user path. Missing module metadata uses stable
  defaults so ordinary SDK `ovti,ov5647` nodes remain probe-compatible.
- The optional `vdd` regulator controls the complete module's 3.3 V high-side
  switch. Only `-ENODEV` means that no controllable regulator is present;
  other regulator lookup failures are returned to the caller.
- `pwdn-gpios` remains optional for compatibility, but the formal AICPM V1 DTS
  deliberately does not bind PWDN or reset: the candidate CAM GPIO pins are
  not electrically verified and must not become safety-critical assumptions.
- The board supplies a 25 MHz sensor clock. Its electrical level, startup
  timing, and measured frequency remain first-board validation items.

## ISP limitation

This backport provides sensor-side RAW10 modes only. The SDK currently has no
verified OV5647 ISP32 IQ package, so image-quality tuning, AE/AWB convergence,
and production image-quality acceptance remain blocked on a validated IQ file.

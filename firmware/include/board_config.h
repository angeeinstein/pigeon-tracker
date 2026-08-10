/*
 * Board-specific wiring for a plain ESP32 DevKit (WROOM-32, 38-pin).
 *
 * These are working defaults, chosen to avoid the ESP32's booby traps rather
 * than picked off a diagram. VERIFY THEM AGAINST YOUR OWN WIRING before you
 * flash: nothing here knows what you actually soldered.
 *
 * The rules they follow:
 *
 *   GPIO 6-11    flash. Using them bricks the boot. Never.
 *   GPIO 34-39   input-only AND no internal pull-ups. They look perfect for
 *                endstops and are the worst choice for them: a switch to
 *                ground on GPIO34 floats, so the axis reads "triggered" at
 *                random. Endstops below use pins that do have pull-ups.
 *   GPIO 0,2,5,  strapping pins, sampled at reset. An output that idles high
 *       12,15    here can stop the board booting (GPIO12 especially: high at
 *                reset selects 1.8 V flash and the chip will not start).
 *   GPIO 1,3     UART0 - the serial monitor. Leave them alone.
 *
 * Set any optional input to -1 to disable it. Disabling a max endstop is
 * normal: the axis then homes against its min endstop and relies on soft
 * limits at the far end.
 */

#pragma once

#include "sdkconfig.h"

/* ---- Stepper drivers (STEP/DIR/EN, e.g. TMC2209) ----------------------
 * Grouped so each driver's three wires sit next to each other on the header.
 */
#ifndef PIN_PAN_STEP
#define PIN_PAN_STEP 26
#endif
#ifndef PIN_PAN_DIR
#define PIN_PAN_DIR 25
#endif
#ifndef PIN_PAN_EN
#define PIN_PAN_EN 27
#endif

#ifndef PIN_TILT_STEP
#define PIN_TILT_STEP 33
#endif
#ifndef PIN_TILT_DIR
#define PIN_TILT_DIR 32
#endif
/* GPIO4 rather than 14: GPIO14 emits a PWM burst at boot on many modules,
 * which would briefly enable the driver before the firmware runs. */
#ifndef PIN_TILT_EN
#define PIN_TILT_EN 4
#endif

/* Driver enable polarity. TMC2209 and A4988 both enable on a LOW level. */
#ifndef DRIVER_ENABLE_ACTIVE_LOW
#define DRIVER_ENABLE_ACTIVE_LOW 1
#endif

/* ---- Endstops (-1 disables) -------------------------------------------
 * Wire the switch between the pin and GND; the firmware enables the internal
 * pull-up (endstop_active_low = true, the default). Both pins below have a
 * usable internal pull-up - see the warning about GPIO 34-39 above.
 */
#ifndef PIN_PAN_MIN_ENDSTOP
#define PIN_PAN_MIN_ENDSTOP 21
#endif
#ifndef PIN_PAN_MAX_ENDSTOP
#define PIN_PAN_MAX_ENDSTOP -1
#endif
#ifndef PIN_TILT_MIN_ENDSTOP
#define PIN_TILT_MIN_ENDSTOP 22
#endif
#ifndef PIN_TILT_MAX_ENDSTOP
#define PIN_TILT_MAX_ENDSTOP -1
#endif

/* ---- Water valve ------------------------------------------------------ */
#ifndef PIN_VALVE
#define PIN_VALVE 23
#endif

/*
 * Valve drive polarity. With an N-channel MOSFET low-side switch this is 1
 * (a HIGH level opens the valve). Fit a pull-down resistor at the gate so the
 * valve stays shut while the ESP32 is in reset, and a flyback diode across
 * the solenoid. Verify this against your driver board before connecting
 * water: getting it backwards means the valve is open whenever the board is
 * off.
 */
#ifndef VALVE_ACTIVE_HIGH
#define VALVE_ACTIVE_HIGH 1
#endif

/* ---- Miscellaneous I/O (-1 disables) ----------------------------------
 * E-stop: a normally-closed button between the pin and GND. Normally closed
 * so a cut wire reads as "pressed" rather than as "all fine".
 */
#ifndef PIN_ESTOP
#define PIN_ESTOP 19
#endif
#ifndef ESTOP_ACTIVE_LOW
#define ESTOP_ACTIVE_LOW 1
#endif

/* GPIO2 is the on-board LED on most DevKits. It is a strapping pin, but only
 * its level *at reset* matters and the firmware drives it well after that. */
#ifndef PIN_STATUS_LED
#define PIN_STATUS_LED 2
#endif

/* Reserved for TMC2209 UART configuration; not used by the current firmware,
 * which assumes the drivers are set by their potentiometer/straps. */
#ifndef PIN_TMC_UART_TX
#define PIN_TMC_UART_TX 17
#endif
#ifndef PIN_TMC_UART_RX
#define PIN_TMC_UART_RX 16
#endif

/* Left free for you: GPIO 13, 14, 18, and the input-only 34/35/36/39
 * (usable for anything that does not need a pull-up, e.g. an analogue
 * sensor or a signal with its own external pull-up). */

/* ---- Timing ----------------------------------------------------------- */
/*
 * Step-generation interrupt rate. This is the hard ceiling on step frequency
 * per axis (one step per tick), so it also bounds the maximum speed:
 *   max_deg_per_s = STEP_ISR_HZ / steps_per_deg
 * 20 kHz with 3200 microsteps/rev and no gearing is ~2250 deg/s - far more
 * than the mechanics will tolerate.
 */
#ifndef STEP_ISR_HZ
#define STEP_ISR_HZ 20000
#endif

/* Motion planning rate (velocity ramps, homing state machine). */
#ifndef MOTION_CONTROL_HZ
#define MOTION_CONTROL_HZ 1000
#endif

#ifndef TURRET_PINS_CONFIGURED
#error "GPIO assignments in include/board_config.h have not been confirmed for your board. Check them against your wiring, then define TURRET_PINS_CONFIGURED (see platformio.ini)."
#endif

/*
 * Board-specific wiring.
 *
 * THESE ARE PLACEHOLDERS. Fill them in for your board, then define
 * TURRET_PINS_CONFIGURED (in platformio.ini or here) to allow the build.
 *
 * The guard exists because a wrong STEP/DIR pin is not a compile error and
 * not a runtime error - it is a stepper driving into a hard stop on first
 * boot. Making the build fail is cheaper than making the mechanics fail.
 *
 * Set an optional input to -1 to disable it (max endstops and the external
 * e-stop input are optional; the axis then homes against its min endstop
 * only and relies on soft limits at the far end).
 */

#pragma once

#include "sdkconfig.h"

/* ---- Stepper drivers (STEP/DIR/EN, e.g. TMC2209) ---------------------- */
#ifndef PIN_PAN_STEP
#define PIN_PAN_STEP 25
#endif
#ifndef PIN_PAN_DIR
#define PIN_PAN_DIR 26
#endif
#ifndef PIN_PAN_EN
#define PIN_PAN_EN 27
#endif

#ifndef PIN_TILT_STEP
#define PIN_TILT_STEP 32
#endif
#ifndef PIN_TILT_DIR
#define PIN_TILT_DIR 33
#endif
#ifndef PIN_TILT_EN
#define PIN_TILT_EN 14
#endif

/* Driver enable polarity. TMC2209 and A4988 both enable on a LOW level. */
#ifndef DRIVER_ENABLE_ACTIVE_LOW
#define DRIVER_ENABLE_ACTIVE_LOW 1
#endif

/* ---- Endstops (-1 disables) ------------------------------------------- */
#ifndef PIN_PAN_MIN_ENDSTOP
#define PIN_PAN_MIN_ENDSTOP 34
#endif
#ifndef PIN_PAN_MAX_ENDSTOP
#define PIN_PAN_MAX_ENDSTOP -1
#endif
#ifndef PIN_TILT_MIN_ENDSTOP
#define PIN_TILT_MIN_ENDSTOP 35
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
 * (a HIGH level opens the valve) and the pull-down resistor keeps it closed
 * while the ESP32 is in reset. Verify this against your driver board before
 * connecting water.
 */
#ifndef VALVE_ACTIVE_HIGH
#define VALVE_ACTIVE_HIGH 1
#endif

/* ---- Miscellaneous I/O (-1 disables) ---------------------------------- */
#ifndef PIN_ESTOP
#define PIN_ESTOP -1
#endif
#ifndef ESTOP_ACTIVE_LOW
#define ESTOP_ACTIVE_LOW 1
#endif

#ifndef PIN_STATUS_LED
#define PIN_STATUS_LED 2
#endif

/* Reserved for TMC UART configuration; not used by the current firmware. */
#ifndef PIN_TMC_UART_TX
#define PIN_TMC_UART_TX -1
#endif
#ifndef PIN_TMC_UART_RX
#define PIN_TMC_UART_RX -1
#endif

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
#error "GPIO assignments in include/board_config.h are placeholders. Set them for your board, then define TURRET_PINS_CONFIGURED (see platformio.ini)."
#endif

/*
 * Two-axis motion control.
 *
 * The server sends angles; everything below - trajectory generation, step
 * pulses, limits and homing - happens here, locally, on a timer interrupt
 * that does not care whether the network exists.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "turret_config.h"

typedef enum {
    MOTION_STATE_IDLE = 0,
    MOTION_STATE_MOVING,
    MOTION_STATE_JOGGING,
    MOTION_STATE_HOMING,
    MOTION_STATE_FAULT,
} motion_state_t;

typedef enum {
    MOTION_AXIS_PAN = 1 << 0,
    MOTION_AXIS_TILT = 1 << 1,
    MOTION_AXIS_BOTH = (1 << 0) | (1 << 1),
} motion_axis_mask_t;

typedef struct {
    float pan_deg;
    float tilt_deg;
    float pan_target_deg;
    float tilt_target_deg;
    float pan_rate_deg_s;
    float tilt_rate_deg_s;
    bool moving;
    bool homed;
    bool limit_pan_min;
    bool limit_pan_max;
    bool limit_tilt_min;
    bool limit_tilt_max;
    motion_state_t state;
} motion_status_t;

/** Configure GPIO, the step timer and the control task. Call once at boot. */
esp_err_t motion_init(const turret_config_t *config);

/** Adopt a new configuration (limits, ratios, speeds) at runtime. */
void motion_apply_config(const turret_config_t *config);

/**
 * Absolute move in degrees.
 *
 * Rejected with ESP_ERR_INVALID_STATE unless homed (or the configuration
 * explicitly allows unhomed motion). Targets outside the soft limits are
 * clamped and reported through @p clamped rather than refused.
 */
esp_err_t motion_move_absolute(float pan_deg, float tilt_deg, float max_speed_deg_s,
                               float accel_deg_s2, bool *clamped);

/** Relative move in degrees. Same rules as motion_move_absolute(). */
esp_err_t motion_move_relative(float pan_delta_deg, float tilt_delta_deg,
                               float max_speed_deg_s, bool *clamped);

/**
 * Velocity (joystick) control.
 *
 * The motion stops on its own @p ttl_ms after the last call: a dropped packet
 * or a closed browser tab decelerates the turret instead of running it into a
 * limit.
 */
esp_err_t motion_jog(float pan_rate_deg_s, float tilt_rate_deg_s, uint32_t ttl_ms);

/** Controlled stop (ramp down) or emergency stop (immediate, clears homing). */
void motion_stop(bool emergency);

/** Clear a latched emergency stop. Homing is still required afterwards. */
void motion_clear_estop(void);

/** True while an emergency stop is latched. */
bool motion_estop_active(void);

/** Begin homing. Returns ESP_ERR_INVALID_STATE if already homing. */
esp_err_t motion_home_start(motion_axis_mask_t axes);

/**
 * Block until the homing started by motion_home_start() finishes.
 *
 * @return ESP_OK on success, ESP_ERR_TIMEOUT if the endstop was never found.
 */
esp_err_t motion_home_wait(uint32_t timeout_ms);

bool motion_is_homed(void);

void motion_get_status(motion_status_t *out);

/** Enable or disable the stepper drivers (holding torque). */
void motion_set_drivers_enabled(bool enabled);

/*
 * Runtime configuration.
 *
 * One struct, one place. Nothing in the firmware hardcodes a gear ratio, a
 * travel limit or a spray duration: everything comes from here, defaults are
 * conservative, the server can update it over the protocol, and it persists
 * in NVS across reboots.
 *
 * Field names match the protocol's config keys exactly (docs/PROTOCOL.md §6).
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    /* -- mechanics -- */
    uint16_t steps_per_rev;      /* motor full steps per revolution        */
    uint16_t pan_microsteps;     /* driver microstepping                   */
    uint16_t tilt_microsteps;
    float pan_gear_ratio;        /* motor revolutions per output revolution */
    float tilt_gear_ratio;
    bool pan_invert;
    bool tilt_invert;

    /* -- travel limits (degrees) -- */
    float pan_min_deg;
    float pan_max_deg;
    float tilt_min_deg;
    float tilt_max_deg;

    /* -- dynamics -- */
    float max_speed_deg_s;
    float accel_deg_s2;

    /* -- homing -- */
    float homing_speed_deg_s;
    float homing_backoff_deg;
    int8_t pan_home_dir;         /* -1 toward the min endstop, +1 toward max */
    int8_t tilt_home_dir;
    float pan_home_offset_deg;   /* angle assigned at the pan endstop        */
    float tilt_home_offset_deg;
    bool endstop_active_low;
    bool allow_unhomed_motion;

    /* -- output -- */
    uint32_t max_spray_ms;       /* hard clamp on a single burst             */

    /* -- link -- */
    uint32_t link_timeout_ms;    /* failsafe when the server goes quiet      */
    uint32_t status_interval_ms;
} turret_config_t;

/** Populate with compiled-in defaults (safe, slow, tight limits). */
void turret_config_defaults(turret_config_t *config);

/** Load from NVS, falling back to defaults when absent or corrupt. */
esp_err_t turret_config_load(turret_config_t *config);

/** Persist to NVS. */
esp_err_t turret_config_save(const turret_config_t *config);

/**
 * Apply a partial JSON update ({"key": value, ...}).
 *
 * Unknown keys are ignored (forward compatibility); out-of-range values are
 * rejected and leave the field untouched. Returns the number of fields that
 * changed, or -1 if the document was not an object.
 */
int turret_config_apply_json(turret_config_t *config, const void *json_object);

/** Serialise the whole configuration into a new cJSON object (caller frees). */
void *turret_config_to_json(const turret_config_t *config);

/** Microsteps per output degree for each axis. */
float turret_config_pan_steps_per_deg(const turret_config_t *config);
float turret_config_tilt_steps_per_deg(const turret_config_t *config);

/*
 * Water valve control.
 *
 * The valve is the one output that can do damage if it sticks on, so it gets
 * more machinery than its one GPIO suggests:
 *
 *   - the pin is driven to its inactive level *before* it becomes an output,
 *     so a reset never produces a pulse;
 *   - opening it always arms a one-shot hardware timer first, so the close is
 *     already scheduled before the water starts;
 *   - it can only open while the output is armed, and disarming closes it;
 *   - the link watchdog and the task watchdog both close it independently.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/** Configure the GPIO in its safe (closed) state. Call before anything else. */
esp_err_t valve_init(uint32_t max_open_ms);

/** Update the hard clamp on a single burst. */
void valve_set_max_open_ms(uint32_t max_open_ms);

/** Arm or disarm the output. Disarming closes the valve immediately. */
void valve_set_armed(bool armed);

bool valve_is_armed(void);

/**
 * Open the valve for @p duration_ms (clamped to the configured maximum).
 *
 * @param[out] applied_ms the duration actually used.
 * @return ESP_ERR_INVALID_STATE when the output is disarmed.
 */
esp_err_t valve_open(uint32_t duration_ms, uint32_t *applied_ms);

/** Close the valve now, whatever the timer says. Always safe to call. */
void valve_close(void);

bool valve_is_open(void);

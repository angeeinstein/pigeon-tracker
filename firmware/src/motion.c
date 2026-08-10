/*
 * Motion control implementation.
 *
 * Two layers:
 *
 *   1. A 20 kHz timer ISR that does nothing but emit step pulses at the
 *      currently commanded rate, and refuses to step past an endstop or a
 *      soft limit. It is short, allocated in IRAM, and holds no locks.
 *   2. A 1 kHz control task that plans: velocity ramps, trapezoidal moves,
 *      jog timeouts and the homing state machine. It writes the ISR's rate
 *      through single 32-bit stores, which are atomic on this core.
 *
 * Keeping the planner out of the ISR is what makes the maths readable; keeping
 * the pulse generation out of the planner is what makes the motion smooth when
 * Wi-Fi decides to spend 40 ms in an interrupt.
 */

#include "motion.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "board_config.h"
#include "driver/gpio.h"
#include "driver/gptimer.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

static const char *TAG = "motion";

#define Q16 65536u
#define HOME_PHASE_TIMEOUT_MS 30000
#define AXIS_COUNT 2

typedef enum { AXIS_PAN = 0, AXIS_TILT = 1 } axis_index_t;

typedef enum {
    HOMING_IDLE = 0,
    HOMING_SEEK,
    HOMING_BACKOFF,
    HOMING_RESEEK,
    HOMING_SETTLE,
    HOMING_NEXT,
    HOMING_DONE,
    HOMING_FAILED,
} homing_phase_t;

typedef struct {
    /* --- shared with the ISR (single-word accesses only) --- */
    volatile int32_t position;   /* microsteps                         */
    volatile uint32_t accum;     /* Q16 fractional step accumulator    */
    volatile uint32_t inc;       /* Q16 steps per ISR tick             */
    volatile int8_t dir;         /* +1 / -1, logical direction         */
    volatile bool pulse_high;
    volatile bool blocked;       /* last step was refused by a limit   */

    /* --- planner state (control task only) --- */
    int32_t target;              /* microsteps                         */
    float velocity;              /* deg/s, signed                      */
    float rate_command;          /* jog rate; NAN when not jogging     */
    float max_speed;
    float accel;
    float steps_per_deg;
    int32_t min_steps;
    int32_t max_steps;
    bool invert;
    int8_t home_dir;
    float home_offset_deg;

    /* --- wiring --- */
    int step_pin;
    int dir_pin;
    int en_pin;
    int min_endstop_pin;
    int max_endstop_pin;

    bool limit_min_hit;
    bool limit_max_hit;
} axis_t;

static axis_t s_axes[AXIS_COUNT];
static turret_config_t s_config;
static gptimer_handle_t s_step_timer;
static EventGroupHandle_t s_events;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

static volatile bool s_limits_enabled = false; /* off until homed */
static volatile bool s_homed = false;
static volatile bool s_estop = false;
static volatile motion_state_t s_state = MOTION_STATE_IDLE;
static volatile int64_t s_jog_expires_us = 0;

static homing_phase_t s_homing_phase = HOMING_IDLE;
static motion_axis_mask_t s_homing_axes = 0;
static int s_homing_axis = 0;
static int64_t s_homing_phase_started_us = 0;

#define EVENT_HOMING_OK (1 << 0)
#define EVENT_HOMING_FAIL (1 << 1)

/* ---------------------------------------------------------------------- */
/* helpers                                                                 */
/* ---------------------------------------------------------------------- */

static inline float clampf(float value, float low, float high)
{
    return value < low ? low : (value > high ? high : value);
}

static inline int32_t deg_to_steps(const axis_t *axis, float deg)
{
    return (int32_t)lroundf(deg * axis->steps_per_deg);
}

static inline float steps_to_deg(const axis_t *axis, int32_t steps)
{
    return axis->steps_per_deg > 0.0f ? (float)steps / axis->steps_per_deg : 0.0f;
}

/* Endstop read. Kept inline and register-only so it is safe from the ISR. */
static inline bool IRAM_ATTR endstop_active(int pin, bool active_low)
{
    if (pin < 0) {
        return false;
    }
    int level = gpio_get_level((gpio_num_t)pin);
    return active_low ? (level == 0) : (level != 0);
}

static inline bool IRAM_ATTR step_allowed(const axis_t *axis, int8_t dir, bool active_low)
{
    if (dir < 0) {
        if (endstop_active(axis->min_endstop_pin, active_low)) {
            return false;
        }
        if (s_limits_enabled && axis->position <= axis->min_steps) {
            return false;
        }
    } else {
        if (endstop_active(axis->max_endstop_pin, active_low)) {
            return false;
        }
        if (s_limits_enabled && axis->position >= axis->max_steps) {
            return false;
        }
    }
    return true;
}

/* ---------------------------------------------------------------------- */
/* step ISR                                                                */
/* ---------------------------------------------------------------------- */

static bool IRAM_ATTR step_isr(gptimer_handle_t timer, const gptimer_alarm_event_data_t *event,
                               void *user_data)
{
    (void)timer;
    (void)event;
    (void)user_data;

    const bool active_low = s_config.endstop_active_low;

    for (int i = 0; i < AXIS_COUNT; i++) {
        axis_t *axis = &s_axes[i];

        /* End the previous pulse: one ISR period of high time (50 us at
         * 20 kHz) is far more than any STEP/DIR driver needs. */
        if (axis->pulse_high) {
            gpio_set_level((gpio_num_t)axis->step_pin, 0);
            axis->pulse_high = false;
        }

        uint32_t inc = axis->inc;
        if (inc == 0 || s_estop) {
            continue;
        }

        uint32_t accum = axis->accum + inc;
        if (accum < Q16) {
            axis->accum = accum;
            continue;
        }
        axis->accum = accum - Q16;

        int8_t dir = axis->dir;
        if (!step_allowed(axis, dir, active_low)) {
            axis->blocked = true;
            axis->inc = 0;
            continue;
        }

        axis->position += dir;
        gpio_set_level((gpio_num_t)axis->step_pin, 1);
        axis->pulse_high = true;
    }

    return false; /* no task woken */
}

/* ---------------------------------------------------------------------- */
/* planner                                                                 */
/* ---------------------------------------------------------------------- */

static void axis_set_rate(axis_t *axis, float deg_per_s)
{
    float magnitude = fabsf(deg_per_s);
    int8_t dir = deg_per_s >= 0.0f ? 1 : -1;

    /* Write direction before the rate so the ISR can never step the old
     * direction at the new speed. */
    if (dir != axis->dir) {
        axis->dir = dir;
        bool level = (dir > 0) != axis->invert;
        gpio_set_level((gpio_num_t)axis->dir_pin, level ? 1 : 0);
        /* Direction setup time for the driver. */
        esp_rom_delay_us(5);
    }

    float steps_per_s = magnitude * axis->steps_per_deg;
    uint32_t inc = (uint32_t)((steps_per_s * (float)Q16) / (float)STEP_ISR_HZ);
    if (inc > Q16) {
        inc = Q16; /* one step per tick is the hardware ceiling */
    }
    axis->inc = inc;
}

/** Desired velocity for a trapezoidal approach to the target. */
static float plan_position_velocity(const axis_t *axis)
{
    float error_deg = steps_to_deg(axis, axis->target - axis->position);
    if (fabsf(error_deg) < 0.005f) {
        return 0.0f;
    }
    /* Never travel faster than the remaining distance allows us to decelerate
     * from - this is the whole trapezoid in one line. */
    float approach = sqrtf(2.0f * axis->accel * fabsf(error_deg));
    float speed = fminf(axis->max_speed, approach);
    return error_deg > 0.0f ? speed : -speed;
}

static void axis_update(axis_t *axis, float dt)
{
    float desired;
    if (!isnan(axis->rate_command)) {
        desired = clampf(axis->rate_command, -axis->max_speed, axis->max_speed);
    } else {
        desired = plan_position_velocity(axis);
    }

    if (s_estop) {
        axis->velocity = 0.0f;
        axis->inc = 0;
        return;
    }

    float max_delta = axis->accel * dt;
    float delta = clampf(desired - axis->velocity, -max_delta, max_delta);
    axis->velocity += delta;

    if (fabsf(axis->velocity) < 0.01f) {
        axis->velocity = 0.0f;
        axis->inc = 0;
        return;
    }

    /* Refuse to drive into an endstop that is already pressed. */
    bool active_low = s_config.endstop_active_low;
    if ((axis->velocity < 0 && endstop_active(axis->min_endstop_pin, active_low)) ||
        (axis->velocity > 0 && endstop_active(axis->max_endstop_pin, active_low))) {
        axis->velocity = 0.0f;
        axis->inc = 0;
        return;
    }

    axis_set_rate(axis, axis->velocity);
}

static bool axis_at_rest(const axis_t *axis)
{
    return fabsf(axis->velocity) < 0.02f && labs(axis->target - axis->position) < 2;
}

/* ---------------------------------------------------------------------- */
/* homing                                                                  */
/* ---------------------------------------------------------------------- */

static void homing_enter(homing_phase_t phase)
{
    s_homing_phase = phase;
    s_homing_phase_started_us = esp_timer_get_time();
}

static bool homing_phase_expired(void)
{
    return (esp_timer_get_time() - s_homing_phase_started_us) >
           (int64_t)HOME_PHASE_TIMEOUT_MS * 1000;
}

static void homing_fail(const char *reason)
{
    ESP_LOGE(TAG, "homing failed: %s", reason);
    for (int i = 0; i < AXIS_COUNT; i++) {
        s_axes[i].rate_command = NAN;
        s_axes[i].velocity = 0.0f;
        s_axes[i].inc = 0;
        s_axes[i].target = s_axes[i].position;
    }
    s_homing_phase = HOMING_IDLE;
    s_homed = false;
    s_state = MOTION_STATE_FAULT;
    xEventGroupSetBits(s_events, EVENT_HOMING_FAIL);
}

static void homing_step(void)
{
    axis_t *axis = &s_axes[s_homing_axis];
    bool active_low = s_config.endstop_active_low;
    int endstop_pin = axis->home_dir < 0 ? axis->min_endstop_pin : axis->max_endstop_pin;

    switch (s_homing_phase) {
    case HOMING_SEEK:
        if (endstop_pin < 0) {
            homing_fail("no endstop configured for the homing direction");
            return;
        }
        if (endstop_active(endstop_pin, active_low)) {
            axis->rate_command = 0.0f;
            axis->velocity = 0.0f;
            axis->inc = 0;
            axis->position = deg_to_steps(axis, axis->home_offset_deg);
            axis->target = axis->position;
            axis->rate_command = NAN;
            /* Back off far enough to release the switch, then approach again
             * slowly: the second touch is the one that is repeatable. */
            axis->target = axis->position - (int32_t)(axis->home_dir *
                                                      deg_to_steps(axis, s_config.homing_backoff_deg));
            homing_enter(HOMING_BACKOFF);
            return;
        }
        axis->rate_command = (float)axis->home_dir * s_config.homing_speed_deg_s;
        if (homing_phase_expired()) {
            homing_fail("endstop not reached");
        }
        return;

    case HOMING_BACKOFF:
        if (axis_at_rest(axis)) {
            homing_enter(HOMING_RESEEK);
        } else if (homing_phase_expired()) {
            homing_fail("back-off did not complete");
        }
        return;

    case HOMING_RESEEK:
        if (endstop_active(endstop_pin, active_low)) {
            axis->rate_command = NAN;
            axis->velocity = 0.0f;
            axis->inc = 0;
            axis->position = deg_to_steps(axis, axis->home_offset_deg);
            axis->target = axis->position;
            homing_enter(HOMING_SETTLE);
            return;
        }
        axis->rate_command = (float)axis->home_dir * s_config.homing_speed_deg_s * 0.25f;
        if (homing_phase_expired()) {
            homing_fail("endstop not reached on the slow approach");
        }
        return;

    case HOMING_SETTLE:
        axis->rate_command = NAN;
        if (axis_at_rest(axis)) {
            homing_enter(HOMING_NEXT);
        }
        return;

    case HOMING_NEXT: {
        /* Home the axes one after another: a wrong direction on one axis
         * should not be discovered while the other is also moving. */
        int next = s_homing_axis + 1;
        while (next < AXIS_COUNT && !(s_homing_axes & (1 << next))) {
            next++;
        }
        if (next < AXIS_COUNT) {
            s_homing_axis = next;
            homing_enter(HOMING_SEEK);
            return;
        }
        s_homed = true;
        s_limits_enabled = true;
        s_homing_phase = HOMING_IDLE;
        s_state = MOTION_STATE_IDLE;
        ESP_LOGI(TAG, "homing completed");
        xEventGroupSetBits(s_events, EVENT_HOMING_OK);
        return;
    }

    default:
        return;
    }
}

/* ---------------------------------------------------------------------- */
/* control task                                                            */
/* ---------------------------------------------------------------------- */

static void control_task(void *arg)
{
    (void)arg;
    const TickType_t period = pdMS_TO_TICKS(1000 / MOTION_CONTROL_HZ);
    const float dt = 1.0f / (float)MOTION_CONTROL_HZ;
    TickType_t last_wake = xTaskGetTickCount();

    while (true) {
        int64_t now_us = esp_timer_get_time();

        /* A jog that is no longer being refreshed decays to a stop. */
        if (s_jog_expires_us != 0 && now_us > s_jog_expires_us) {
            s_jog_expires_us = 0;
            for (int i = 0; i < AXIS_COUNT; i++) {
                s_axes[i].rate_command = NAN;
                s_axes[i].target = s_axes[i].position;
            }
        }

        if (s_homing_phase != HOMING_IDLE) {
            s_state = MOTION_STATE_HOMING;
            homing_step();
        }

        bool moving = false;
        for (int i = 0; i < AXIS_COUNT; i++) {
            axis_update(&s_axes[i], dt);
            if (fabsf(s_axes[i].velocity) > 0.02f) {
                moving = true;
            }
            axis_t *axis = &s_axes[i];
            axis->limit_min_hit = endstop_active(axis->min_endstop_pin,
                                                 s_config.endstop_active_low);
            axis->limit_max_hit = endstop_active(axis->max_endstop_pin,
                                                 s_config.endstop_active_low);
        }

        if (s_estop) {
            s_state = MOTION_STATE_FAULT;
        } else if (s_homing_phase != HOMING_IDLE) {
            s_state = MOTION_STATE_HOMING;
        } else if (s_jog_expires_us != 0) {
            s_state = MOTION_STATE_JOGGING;
        } else if (moving) {
            s_state = MOTION_STATE_MOVING;
        } else {
            s_state = MOTION_STATE_IDLE;
        }

        vTaskDelayUntil(&last_wake, period);
    }
}

/* ---------------------------------------------------------------------- */
/* public API                                                              */
/* ---------------------------------------------------------------------- */

static void configure_output(int pin, int initial_level)
{
    if (pin < 0) {
        return;
    }
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_set_level((gpio_num_t)pin, initial_level);
    ESP_ERROR_CHECK(gpio_config(&io));
    gpio_set_level((gpio_num_t)pin, initial_level);
}

static void configure_endstop(int pin, bool active_low)
{
    if (pin < 0) {
        return;
    }
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = active_low ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE,
        .pull_down_en = active_low ? GPIO_PULLDOWN_DISABLE : GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io));
}

void motion_apply_config(const turret_config_t *config)
{
    taskENTER_CRITICAL(&s_mux);
    s_config = *config;

    axis_t *pan = &s_axes[AXIS_PAN];
    axis_t *tilt = &s_axes[AXIS_TILT];

    pan->steps_per_deg = turret_config_pan_steps_per_deg(config);
    tilt->steps_per_deg = turret_config_tilt_steps_per_deg(config);
    pan->invert = config->pan_invert;
    tilt->invert = config->tilt_invert;
    pan->max_speed = config->max_speed_deg_s;
    tilt->max_speed = config->max_speed_deg_s;
    pan->accel = config->accel_deg_s2;
    tilt->accel = config->accel_deg_s2;
    pan->home_dir = config->pan_home_dir;
    tilt->home_dir = config->tilt_home_dir;
    pan->home_offset_deg = config->pan_home_offset_deg;
    tilt->home_offset_deg = config->tilt_home_offset_deg;
    pan->min_steps = deg_to_steps(pan, config->pan_min_deg);
    pan->max_steps = deg_to_steps(pan, config->pan_max_deg);
    tilt->min_steps = deg_to_steps(tilt, config->tilt_min_deg);
    tilt->max_steps = deg_to_steps(tilt, config->tilt_max_deg);
    taskEXIT_CRITICAL(&s_mux);

    ESP_LOGI(TAG, "configuration applied: pan %.2f steps/deg, tilt %.2f steps/deg",
             pan->steps_per_deg, tilt->steps_per_deg);
}

esp_err_t motion_init(const turret_config_t *config)
{
    memset(s_axes, 0, sizeof(s_axes));
    s_events = xEventGroupCreate();
    if (!s_events) {
        return ESP_ERR_NO_MEM;
    }

    axis_t *pan = &s_axes[AXIS_PAN];
    axis_t *tilt = &s_axes[AXIS_TILT];

    pan->step_pin = PIN_PAN_STEP;
    pan->dir_pin = PIN_PAN_DIR;
    pan->en_pin = PIN_PAN_EN;
    pan->min_endstop_pin = PIN_PAN_MIN_ENDSTOP;
    pan->max_endstop_pin = PIN_PAN_MAX_ENDSTOP;
    pan->rate_command = NAN;
    pan->dir = 1;

    tilt->step_pin = PIN_TILT_STEP;
    tilt->dir_pin = PIN_TILT_DIR;
    tilt->en_pin = PIN_TILT_EN;
    tilt->min_endstop_pin = PIN_TILT_MIN_ENDSTOP;
    tilt->max_endstop_pin = PIN_TILT_MAX_ENDSTOP;
    tilt->rate_command = NAN;
    tilt->dir = 1;

    for (int i = 0; i < AXIS_COUNT; i++) {
        configure_output(s_axes[i].step_pin, 0);
        configure_output(s_axes[i].dir_pin, 0);
        configure_output(s_axes[i].en_pin, DRIVER_ENABLE_ACTIVE_LOW ? 1 : 0);
        configure_endstop(s_axes[i].min_endstop_pin, config->endstop_active_low);
        configure_endstop(s_axes[i].max_endstop_pin, config->endstop_active_low);
    }

    motion_apply_config(config);

    gptimer_config_t timer_config = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000, /* 1 us ticks */
    };
    ESP_ERROR_CHECK(gptimer_new_timer(&timer_config, &s_step_timer));

    gptimer_event_callbacks_t callbacks = {.on_alarm = step_isr};
    ESP_ERROR_CHECK(gptimer_register_event_callbacks(s_step_timer, &callbacks, NULL));

    gptimer_alarm_config_t alarm = {
        .alarm_count = 1000000 / STEP_ISR_HZ,
        .reload_count = 0,
        .flags.auto_reload_on_alarm = true,
    };
    ESP_ERROR_CHECK(gptimer_set_alarm_action(s_step_timer, &alarm));
    ESP_ERROR_CHECK(gptimer_enable(s_step_timer));
    ESP_ERROR_CHECK(gptimer_start(s_step_timer));

    motion_set_drivers_enabled(true);

    BaseType_t ok = xTaskCreate(control_task, "motion", 4096, NULL, 10, NULL);
    if (ok != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "motion ready (%d Hz step ISR, %d Hz planner)", STEP_ISR_HZ,
             MOTION_CONTROL_HZ);
    return ESP_OK;
}

void motion_set_drivers_enabled(bool enabled)
{
    int level = DRIVER_ENABLE_ACTIVE_LOW ? (enabled ? 0 : 1) : (enabled ? 1 : 0);
    for (int i = 0; i < AXIS_COUNT; i++) {
        if (s_axes[i].en_pin >= 0) {
            gpio_set_level((gpio_num_t)s_axes[i].en_pin, level);
        }
    }
}

static esp_err_t check_can_move(void)
{
    if (s_estop) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_homing_phase != HOMING_IDLE) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_homed && !s_config.allow_unhomed_motion) {
        return ESP_ERR_NOT_ALLOWED;
    }
    return ESP_OK;
}

esp_err_t motion_move_absolute(float pan_deg, float tilt_deg, float max_speed_deg_s,
                               float accel_deg_s2, bool *clamped)
{
    esp_err_t err = check_can_move();
    if (err != ESP_OK) {
        return err;
    }

    float pan_clamped = clampf(pan_deg, s_config.pan_min_deg, s_config.pan_max_deg);
    float tilt_clamped = clampf(tilt_deg, s_config.tilt_min_deg, s_config.tilt_max_deg);
    if (clamped) {
        *clamped = (fabsf(pan_clamped - pan_deg) > 1e-3f) ||
                   (fabsf(tilt_clamped - tilt_deg) > 1e-3f);
    }

    taskENTER_CRITICAL(&s_mux);
    if (max_speed_deg_s > 0.0f) {
        s_axes[AXIS_PAN].max_speed = fminf(max_speed_deg_s, s_config.max_speed_deg_s);
        s_axes[AXIS_TILT].max_speed = s_axes[AXIS_PAN].max_speed;
    }
    if (accel_deg_s2 > 0.0f) {
        s_axes[AXIS_PAN].accel = fminf(accel_deg_s2, s_config.accel_deg_s2);
        s_axes[AXIS_TILT].accel = s_axes[AXIS_PAN].accel;
    }
    s_axes[AXIS_PAN].rate_command = NAN;
    s_axes[AXIS_TILT].rate_command = NAN;
    s_jog_expires_us = 0;
    s_axes[AXIS_PAN].target = deg_to_steps(&s_axes[AXIS_PAN], pan_clamped);
    s_axes[AXIS_TILT].target = deg_to_steps(&s_axes[AXIS_TILT], tilt_clamped);
    taskEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}

esp_err_t motion_move_relative(float pan_delta_deg, float tilt_delta_deg,
                               float max_speed_deg_s, bool *clamped)
{
    float pan_now = steps_to_deg(&s_axes[AXIS_PAN], s_axes[AXIS_PAN].position);
    float tilt_now = steps_to_deg(&s_axes[AXIS_TILT], s_axes[AXIS_TILT].position);
    return motion_move_absolute(pan_now + pan_delta_deg, tilt_now + tilt_delta_deg,
                                max_speed_deg_s, 0.0f, clamped);
}

esp_err_t motion_jog(float pan_rate_deg_s, float tilt_rate_deg_s, uint32_t ttl_ms)
{
    if (s_estop || s_homing_phase != HOMING_IDLE) {
        return ESP_ERR_INVALID_STATE;
    }

    taskENTER_CRITICAL(&s_mux);
    if (pan_rate_deg_s == 0.0f && tilt_rate_deg_s == 0.0f) {
        s_axes[AXIS_PAN].rate_command = 0.0f;
        s_axes[AXIS_TILT].rate_command = 0.0f;
        s_jog_expires_us = esp_timer_get_time() + 200000; /* ramp down, then idle */
    } else {
        s_axes[AXIS_PAN].rate_command = pan_rate_deg_s;
        s_axes[AXIS_TILT].rate_command = tilt_rate_deg_s;
        s_jog_expires_us = esp_timer_get_time() + (int64_t)ttl_ms * 1000;
    }
    taskEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}

void motion_stop(bool emergency)
{
    taskENTER_CRITICAL(&s_mux);
    s_jog_expires_us = 0;
    for (int i = 0; i < AXIS_COUNT; i++) {
        s_axes[i].rate_command = emergency ? 0.0f : NAN;
        s_axes[i].target = s_axes[i].position;
        if (emergency) {
            s_axes[i].velocity = 0.0f;
            s_axes[i].inc = 0;
            s_axes[i].rate_command = NAN;
        }
    }
    if (emergency) {
        s_estop = true;
        /* Steps were certainly lost during a hard stop, so the position is no
         * longer trustworthy: force a re-home before absolute motion. */
        s_homed = false;
        s_limits_enabled = false;
        s_homing_phase = HOMING_IDLE;
    }
    taskEXIT_CRITICAL(&s_mux);

    if (emergency) {
        ESP_LOGW(TAG, "emergency stop: homing invalidated");
        xEventGroupSetBits(s_events, EVENT_HOMING_FAIL);
    }
}

void motion_clear_estop(void)
{
    s_estop = false;
    s_state = MOTION_STATE_IDLE;
    ESP_LOGI(TAG, "emergency stop cleared (re-home before absolute motion)");
}

bool motion_estop_active(void)
{
    return s_estop;
}

esp_err_t motion_home_start(motion_axis_mask_t axes)
{
    if (s_estop) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_homing_phase != HOMING_IDLE) {
        return ESP_ERR_INVALID_STATE;
    }
    if (axes == 0) {
        axes = MOTION_AXIS_BOTH;
    }

    xEventGroupClearBits(s_events, EVENT_HOMING_OK | EVENT_HOMING_FAIL);

    taskENTER_CRITICAL(&s_mux);
    s_homing_axes = axes;
    s_homing_axis = (axes & MOTION_AXIS_PAN) ? AXIS_PAN : AXIS_TILT;
    s_homed = false;
    /* Soft limits are relative to a reference we do not have yet. */
    s_limits_enabled = false;
    s_jog_expires_us = 0;
    for (int i = 0; i < AXIS_COUNT; i++) {
        s_axes[i].rate_command = NAN;
        s_axes[i].target = s_axes[i].position;
    }
    taskEXIT_CRITICAL(&s_mux);

    homing_enter(HOMING_SEEK);
    ESP_LOGI(TAG, "homing started (mask=0x%02x)", axes);
    return ESP_OK;
}

esp_err_t motion_home_wait(uint32_t timeout_ms)
{
    EventBits_t bits = xEventGroupWaitBits(s_events, EVENT_HOMING_OK | EVENT_HOMING_FAIL, pdTRUE,
                                           pdFALSE, pdMS_TO_TICKS(timeout_ms));
    if (bits & EVENT_HOMING_OK) {
        return ESP_OK;
    }
    if (bits & EVENT_HOMING_FAIL) {
        return ESP_FAIL;
    }
    s_homing_phase = HOMING_IDLE;
    motion_stop(false);
    return ESP_ERR_TIMEOUT;
}

bool motion_is_homed(void)
{
    return s_homed;
}

void motion_get_status(motion_status_t *out)
{
    if (!out) {
        return;
    }
    const axis_t *pan = &s_axes[AXIS_PAN];
    const axis_t *tilt = &s_axes[AXIS_TILT];

    out->pan_deg = steps_to_deg(pan, pan->position);
    out->tilt_deg = steps_to_deg(tilt, tilt->position);
    out->pan_target_deg = steps_to_deg(pan, pan->target);
    out->tilt_target_deg = steps_to_deg(tilt, tilt->target);
    out->pan_rate_deg_s = pan->velocity;
    out->tilt_rate_deg_s = tilt->velocity;
    out->moving = fabsf(pan->velocity) > 0.02f || fabsf(tilt->velocity) > 0.02f;
    out->homed = s_homed;
    out->limit_pan_min = pan->limit_min_hit;
    out->limit_pan_max = pan->limit_max_hit;
    out->limit_tilt_min = tilt->limit_min_hit;
    out->limit_tilt_max = tilt->limit_max_hit;
    out->state = s_state;
}

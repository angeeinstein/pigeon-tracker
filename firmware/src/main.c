/*
 * Turret controller firmware — entry point.
 *
 * Boot order is deliberate and safety-first:
 *
 *   1. valve_init()  — the valve GPIO is driven to CLOSED before anything
 *                      else runs, so a reset can never leave water flowing.
 *   2. NVS + config  — load persisted mechanics/limits (or safe defaults).
 *   3. motion_init() — step timer and planner, drivers enabled, unhomed.
 *   4. network       — Wi-Fi, then the WebSocket client to the server.
 *   5. safety task   — e-stop input, task watchdog, status LED.
 *
 * Nothing here decides *what* the turret should do; that is the server's job.
 * This firmware decides what it is allowed to do, and what happens when the
 * server stops talking.
 */

#include <stdio.h>

#include "board_config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "motion.h"
#include "nvs_flash.h"
#include "protocol_generated.h"
#include "turret_config.h"
#include "valve.h"
#include "wifi_manager.h"
#include "ws_client.h"

#ifndef TURRET_FIRMWARE_VERSION
#define TURRET_FIRMWARE_VERSION "0.0.0"
#endif

static const char *TAG = "main";

static turret_config_t s_config;

/* Debounce for the optional external e-stop input, milliseconds. */
#define ESTOP_DEBOUNCE_MS 20
#define SAFETY_PERIOD_MS 50

static bool estop_input_asserted(void)
{
    if (PIN_ESTOP < 0) {
        return false;
    }
    int level = gpio_get_level((gpio_num_t)PIN_ESTOP);
    return ESTOP_ACTIVE_LOW ? (level == 0) : (level != 0);
}

static void configure_estop_input(void)
{
    if (PIN_ESTOP < 0) {
        ESP_LOGW(TAG, "no external e-stop input configured");
        return;
    }
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PIN_ESTOP,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = ESTOP_ACTIVE_LOW ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE,
        .pull_down_en = ESTOP_ACTIVE_LOW ? GPIO_PULLDOWN_DISABLE : GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io));
}

static void configure_status_led(void)
{
    if (PIN_STATUS_LED < 0) {
        return;
    }
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PIN_STATUS_LED,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io));
}

/**
 * Safety task.
 *
 * Subscribed to the task watchdog: if this task stops running, the chip
 * resets, every GPIO returns to its reset state and the valve closes. That is
 * the last line of defence behind the valve's own one-shot timer and the
 * link timeout in ws_client.
 */
static void safety_task(void *arg)
{
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));

    int estop_stable_ms = 0;
    bool estop_latched = false;
    int64_t led_toggle_us = 0;
    bool led_on = false;

    while (true) {
        esp_task_wdt_reset();

        if (estop_input_asserted()) {
            estop_stable_ms += SAFETY_PERIOD_MS;
            if (estop_stable_ms >= ESTOP_DEBOUNCE_MS && !estop_latched) {
                estop_latched = true;
                ESP_LOGE(TAG, "external emergency stop asserted");
                valve_close();
                valve_set_armed(false);
                motion_stop(true);
                ws_client_send_event(EVT_ESTOP, NULL, 0);
            }
        } else {
            estop_stable_ms = 0;
            estop_latched = false;
        }

        /* Status LED: solid when connected, slow blink while connecting,
         * fast blink on a fault. Enough to diagnose from the balcony door. */
        if (PIN_STATUS_LED >= 0) {
            int64_t now = esp_timer_get_time();
            int64_t period_us;
            if (motion_estop_active()) {
                period_us = 100000;
            } else if (ws_client_is_connected()) {
                period_us = 0;
            } else if (network_is_connected()) {
                period_us = 500000;
            } else {
                period_us = 1000000;
            }

            if (period_us == 0) {
                led_on = true;
                gpio_set_level((gpio_num_t)PIN_STATUS_LED, 1);
            } else if (now - led_toggle_us >= period_us) {
                led_toggle_us = now;
                led_on = !led_on;
                gpio_set_level((gpio_num_t)PIN_STATUS_LED, led_on ? 1 : 0);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(SAFETY_PERIOD_MS));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "turret controller %s (protocol v%d) starting", TURRET_FIRMWARE_VERSION,
             TURRET_PROTOCOL_VERSION);
    ESP_LOGI(TAG, "reset reason: %d", (int)esp_reset_reason());

    /* Before anything else: make the dangerous output safe. */
    ESP_ERROR_CHECK(valve_init(2000));
    configure_estop_input();
    configure_status_led();

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(turret_config_load(&s_config));
    valve_set_max_open_ms(s_config.max_spray_ms);

    ESP_ERROR_CHECK(motion_init(&s_config));

    xTaskCreate(safety_task, "safety", 3072, NULL, 12, NULL);

    ESP_ERROR_CHECK(network_start());
    if (network_wait_connected(30000) != ESP_OK) {
        /* Not fatal: the Wi-Fi layer keeps retrying and the WebSocket client
         * connects as soon as there is a route. The turret stays usable as a
         * safe, stationary lump in the meantime. */
        ESP_LOGW(TAG, "no network yet; continuing to retry in the background");
    }

    ESP_ERROR_CHECK(ws_client_start(&s_config));
    ws_client_send_event(EVT_BOOT, NULL, 0);

    ESP_LOGI(TAG, "ready");
}

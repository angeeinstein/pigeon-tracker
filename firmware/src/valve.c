#include "valve.h"

#include "board_config.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "valve";

#define VALVE_LEVEL_OPEN (VALVE_ACTIVE_HIGH ? 1 : 0)
#define VALVE_LEVEL_CLOSED (VALVE_ACTIVE_HIGH ? 0 : 1)

static esp_timer_handle_t s_close_timer;
static volatile bool s_open;
static volatile bool s_armed;
static uint32_t s_max_open_ms = 2000;

static void IRAM_ATTR close_timer_cb(void *arg)
{
    (void)arg;
    /* Runs from the esp_timer task: this is the guarantee that a burst ends
     * even if every other task in the firmware has stopped running. */
    gpio_set_level((gpio_num_t)PIN_VALVE, VALVE_LEVEL_CLOSED);
    s_open = false;
}

esp_err_t valve_init(uint32_t max_open_ms)
{
    s_max_open_ms = max_open_ms ? max_open_ms : 2000;

    /* Drive the level first, configure as output second. Doing it the other
     * way round produces a short pulse on some pins during boot. */
    gpio_set_level((gpio_num_t)PIN_VALVE, VALVE_LEVEL_CLOSED);
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PIN_VALVE,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = VALVE_ACTIVE_HIGH ? GPIO_PULLUP_DISABLE : GPIO_PULLUP_ENABLE,
        .pull_down_en = VALVE_ACTIVE_HIGH ? GPIO_PULLDOWN_ENABLE : GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&io);
    if (err != ESP_OK) {
        return err;
    }
    gpio_set_level((gpio_num_t)PIN_VALVE, VALVE_LEVEL_CLOSED);

    const esp_timer_create_args_t args = {
        .callback = close_timer_cb,
        .name = "valve_close",
        .dispatch_method = ESP_TIMER_TASK,
    };
    err = esp_timer_create(&args, &s_close_timer);
    if (err != ESP_OK) {
        return err;
    }

    s_open = false;
    s_armed = false;
    ESP_LOGI(TAG, "valve ready on GPIO %d (closed, max burst %u ms)", PIN_VALVE,
             (unsigned)s_max_open_ms);
    return ESP_OK;
}

void valve_set_max_open_ms(uint32_t max_open_ms)
{
    if (max_open_ms > 0) {
        s_max_open_ms = max_open_ms;
    }
}

void valve_set_armed(bool armed)
{
    if (!armed) {
        valve_close();
    }
    s_armed = armed;
    ESP_LOGI(TAG, "output %s", armed ? "armed" : "disarmed");
}

bool valve_is_armed(void)
{
    return s_armed;
}

esp_err_t valve_open(uint32_t duration_ms, uint32_t *applied_ms)
{
    if (!s_armed) {
        return ESP_ERR_INVALID_STATE;
    }

    uint32_t duration = duration_ms;
    if (duration == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (duration > s_max_open_ms) {
        duration = s_max_open_ms;
    }
    if (applied_ms) {
        *applied_ms = duration;
    }

    /* Arm the close before the open. If anything below fails, the worst case
     * is a timer that fires against an already-closed valve. */
    esp_timer_stop(s_close_timer);
    esp_err_t err = esp_timer_start_once(s_close_timer, (uint64_t)duration * 1000);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "refusing to open: close timer could not be armed (%d)", err);
        return err;
    }

    gpio_set_level((gpio_num_t)PIN_VALVE, VALVE_LEVEL_OPEN);
    s_open = true;
    ESP_LOGI(TAG, "valve open for %u ms", (unsigned)duration);
    return ESP_OK;
}

void valve_close(void)
{
    if (s_close_timer) {
        esp_timer_stop(s_close_timer);
    }
    gpio_set_level((gpio_num_t)PIN_VALVE, VALVE_LEVEL_CLOSED);
    if (s_open) {
        ESP_LOGI(TAG, "valve closed");
    }
    s_open = false;
}

bool valve_is_open(void)
{
    return s_open;
}

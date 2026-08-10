#include "wifi_manager.h"

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

#if __has_include("secrets.h")
#include "secrets.h"
#endif

#ifndef TURRET_WIFI_SSID
#define TURRET_WIFI_SSID ""
#endif
#ifndef TURRET_WIFI_PASSWORD
#define TURRET_WIFI_PASSWORD ""
#endif

static const char *TAG = "net";

#define BIT_CONNECTED (1 << 0)

static EventGroupHandle_t s_events;
static char s_ip[16] = "0.0.0.0";
static char s_mac[18] = "00:00:00:00:00:00";
static volatile bool s_connected;
static int s_retry_count;
static esp_timer_handle_t s_reconnect_timer;

static void reconnect_cb(void *arg)
{
    (void)arg;
    esp_wifi_connect();
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;

    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_connected = false;
        strcpy(s_ip, "0.0.0.0");
        xEventGroupClearBits(s_events, BIT_CONNECTED);
        /* Retry forever, backing off after the first few attempts. Scheduled
         * on a timer rather than delayed here: this runs on the event loop
         * task and blocking it would stall every other event in the system. */
        s_retry_count++;
        uint64_t delay_us = (s_retry_count < 10 ? 1000 : 5000) * 1000ULL;
        esp_timer_stop(s_reconnect_timer);
        esp_timer_start_once(s_reconnect_timer, delay_us);
        ESP_LOGW(TAG, "Wi-Fi disconnected, retrying (%d)", s_retry_count);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&event->ip_info.ip));
        s_connected = true;
        s_retry_count = 0;
        ESP_LOGI(TAG, "network up: %s", s_ip);
        xEventGroupSetBits(s_events, BIT_CONNECTED);
    }
}

esp_err_t network_start(void)
{
    s_events = xEventGroupCreate();
    if (!s_events) {
        return ESP_ERR_NO_MEM;
    }

    const esp_timer_create_args_t reconnect_args = {
        .callback = reconnect_cb,
        .name = "wifi_reconnect",
    };
    ESP_ERROR_CHECK(esp_timer_create(&reconnect_args, &s_reconnect_timer));

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &on_wifi_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &on_wifi_event, NULL, NULL));

    if (strlen(TURRET_WIFI_SSID) == 0) {
        ESP_LOGE(TAG, "no Wi-Fi SSID configured - copy include/secrets.example.h to "
                      "include/secrets.h and fill it in");
        return ESP_ERR_INVALID_STATE;
    }

    wifi_config_t config = {0};
    strncpy((char *)config.sta.ssid, TURRET_WIFI_SSID, sizeof(config.sta.ssid) - 1);
    strncpy((char *)config.sta.password, TURRET_WIFI_PASSWORD,
            sizeof(config.sta.password) - 1);
    config.sta.threshold.authmode =
        strlen(TURRET_WIFI_PASSWORD) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    /* Power save costs latency on every packet, which shows up directly as
     * joystick lag. The turret is mains powered; keep the radio awake. */
    config.sta.pmf_cfg.capable = true;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    uint8_t mac[6] = {0};
    if (esp_wifi_get_mac(WIFI_IF_STA, mac) == ESP_OK) {
        snprintf(s_mac, sizeof(s_mac), "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2],
                 mac[3], mac[4], mac[5]);
    }

    ESP_LOGI(TAG, "connecting to \"%s\"", TURRET_WIFI_SSID);
    return ESP_OK;
}

esp_err_t network_wait_connected(uint32_t timeout_ms)
{
    EventBits_t bits =
        xEventGroupWaitBits(s_events, BIT_CONNECTED, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms));
    return (bits & BIT_CONNECTED) ? ESP_OK : ESP_ERR_TIMEOUT;
}

bool network_is_connected(void)
{
    return s_connected;
}

const char *network_ip_address(void)
{
    return s_ip;
}

const char *network_mac_address(void)
{
    return s_mac;
}

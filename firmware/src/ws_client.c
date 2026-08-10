#include "ws_client.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "motion.h"
#include "protocol_generated.h"
#include "valve.h"
#include "wifi_manager.h"

#if CONFIG_MBEDTLS_CERTIFICATE_BUNDLE
#include "esp_crt_bundle.h"
#endif

#if __has_include("secrets.h")
#include "secrets.h"
#endif

#ifndef TURRET_SERVER_URI
#define TURRET_SERVER_URI "ws://192.168.1.10:8080/ws/hardware"
#endif
#ifndef TURRET_CONTROLLER_TOKEN
#define TURRET_CONTROLLER_TOKEN ""
#endif
#ifndef TURRET_CONTROLLER_ID
#define TURRET_CONTROLLER_ID "turret-1"
#endif
#ifndef TURRET_FIRMWARE_VERSION
#define TURRET_FIRMWARE_VERSION "0.0.0"
#endif

static const char *TAG = "ws";

#define SEND_TIMEOUT_MS 2000
#define RX_BUFFER_SIZE 2048

typedef struct {
    int command_id;
    motion_axis_mask_t axes;
} home_request_t;

static esp_websocket_client_handle_t s_client;
static turret_config_t *s_config;
static QueueHandle_t s_home_queue;
static char s_rx_buffer[RX_BUFFER_SIZE];
static size_t s_rx_length;

static volatile bool s_connected;      /* socket open                     */
static volatile bool s_accepted;       /* server accepted the handshake   */
static volatile int64_t s_last_server_frame_us;
static uint32_t s_status_seq;
static int64_t s_boot_us;

/* ---------------------------------------------------------------------- */
/* sending                                                                 */
/* ---------------------------------------------------------------------- */

static void send_json(cJSON *root)
{
    if (!root) {
        return;
    }
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!text) {
        return;
    }
    if (s_client && s_connected) {
        int length = (int)strlen(text);
        if (length <= TURRET_MAX_FRAME_BYTES) {
            esp_websocket_client_send_text(s_client, text, length,
                                           pdMS_TO_TICKS(SEND_TIMEOUT_MS));
        } else {
            ESP_LOGE(TAG, "refusing to send a %d byte frame", length);
        }
    }
    cJSON_free(text);
}

static cJSON *new_message(const char *type)
{
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return NULL;
    }
    cJSON_AddNumberToObject(root, "v", TURRET_PROTOCOL_VERSION);
    cJSON_AddStringToObject(root, "type", type);
    return root;
}

static void send_ack(int command_id, bool ok, const char *code, const char *error, bool clamped)
{
    cJSON *root = new_message(MSG_ACK);
    if (!root) {
        return;
    }
    cJSON_AddNumberToObject(root, "id", command_id);
    cJSON_AddBoolToObject(root, "ok", ok);
    cJSON_AddBoolToObject(root, "clamped", clamped);
    if (code) {
        cJSON_AddStringToObject(root, "code", code);
    }
    if (error) {
        cJSON_AddStringToObject(root, "error", error);
    }
    send_json(root);
}

void ws_client_send_event(const char *event, const char *detail_key, double detail_value)
{
    cJSON *root = new_message(MSG_EVENT);
    if (!root) {
        return;
    }
    cJSON_AddStringToObject(root, "event", event);
    cJSON *detail = cJSON_CreateObject();
    if (detail) {
        if (detail_key) {
            cJSON_AddNumberToObject(detail, detail_key, detail_value);
        }
        cJSON_AddItemToObject(root, "detail", detail);
    }
    send_json(root);
}

static void send_hello(void)
{
    cJSON *root = new_message(MSG_HELLO);
    if (!root) {
        return;
    }
    cJSON_AddStringToObject(root, "controller_id", TURRET_CONTROLLER_ID);
    cJSON_AddStringToObject(root, "firmware_version", TURRET_FIRMWARE_VERSION);
    cJSON_AddNumberToObject(root, "protocol_version", TURRET_PROTOCOL_VERSION);
    if (strlen(TURRET_CONTROLLER_TOKEN) > 0) {
        cJSON_AddStringToObject(root, "token", TURRET_CONTROLLER_TOKEN);
    }

    cJSON *capabilities = cJSON_CreateArray();
    if (capabilities) {
        cJSON_AddItemToArray(capabilities, cJSON_CreateString("pan"));
        cJSON_AddItemToArray(capabilities, cJSON_CreateString("tilt"));
        cJSON_AddItemToArray(capabilities, cJSON_CreateString("valve"));
        cJSON_AddItemToArray(capabilities, cJSON_CreateString("endstops"));
        cJSON_AddItemToObject(root, "capabilities", capabilities);
    }

    cJSON *hardware = cJSON_CreateObject();
    if (hardware) {
        cJSON_AddStringToObject(hardware, "chip", CONFIG_IDF_TARGET);
        cJSON_AddStringToObject(hardware, "mac", network_mac_address());
        cJSON_AddStringToObject(hardware, "ip", network_ip_address());
        cJSON_AddItemToObject(root, "hardware", hardware);
    }

    send_json(root);
}

static void send_status(void)
{
    motion_status_t motion;
    motion_get_status(&motion);

    cJSON *root = new_message(MSG_STATUS);
    if (!root) {
        return;
    }
    cJSON_AddNumberToObject(root, "seq", ++s_status_seq);
    cJSON_AddNumberToObject(root, "uptime_ms", (double)((esp_timer_get_time() - s_boot_us) / 1000));

    const char *state = STATE_IDLE;
    if (motion_estop_active()) {
        state = STATE_ESTOP;
    } else {
        switch (motion.state) {
        case MOTION_STATE_MOVING: state = STATE_MOVING; break;
        case MOTION_STATE_JOGGING: state = STATE_JOGGING; break;
        case MOTION_STATE_HOMING: state = STATE_HOMING; break;
        case MOTION_STATE_FAULT: state = STATE_FAULT; break;
        default: state = STATE_IDLE; break;
        }
    }
    cJSON_AddStringToObject(root, "state", state);
    cJSON_AddNumberToObject(root, "pan_deg", motion.pan_deg);
    cJSON_AddNumberToObject(root, "tilt_deg", motion.tilt_deg);
    cJSON_AddNumberToObject(root, "target_pan_deg", motion.pan_target_deg);
    cJSON_AddNumberToObject(root, "target_tilt_deg", motion.tilt_target_deg);
    cJSON_AddNumberToObject(root, "pan_rate_deg_s", motion.pan_rate_deg_s);
    cJSON_AddNumberToObject(root, "tilt_rate_deg_s", motion.tilt_rate_deg_s);
    cJSON_AddBoolToObject(root, "moving", motion.moving);
    cJSON_AddBoolToObject(root, "homed", motion.homed);
    cJSON_AddBoolToObject(root, "armed", valve_is_armed());
    cJSON_AddBoolToObject(root, "valve_open", valve_is_open());
    cJSON_AddBoolToObject(root, "limit_pan_min", motion.limit_pan_min);
    cJSON_AddBoolToObject(root, "limit_pan_max", motion.limit_pan_max);
    cJSON_AddBoolToObject(root, "limit_tilt_min", motion.limit_tilt_min);
    cJSON_AddBoolToObject(root, "limit_tilt_max", motion.limit_tilt_max);
    cJSON_AddBoolToObject(root, "estop", motion_estop_active());
    cJSON_AddNullToObject(root, "error");
    send_json(root);
}

/* ---------------------------------------------------------------------- */
/* command handling                                                        */
/* ---------------------------------------------------------------------- */

static double json_number(const cJSON *root, const char *key, double fallback)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
    if (cJSON_IsNumber(item) && isfinite(item->valuedouble)) {
        return item->valuedouble;
    }
    return fallback;
}

static esp_err_t motion_err_to_code(esp_err_t err, const char **code, const char **message)
{
    switch (err) {
    case ESP_OK:
        return ESP_OK;
    case ESP_ERR_NOT_ALLOWED:
        *code = ERR_NOT_HOMED;
        *message = "absolute motion requires homing";
        return err;
    case ESP_ERR_INVALID_STATE:
        *code = motion_estop_active() ? ERR_ESTOP : ERR_BUSY;
        *message = motion_estop_active() ? "emergency stop is latched" : "busy";
        return err;
    default:
        *code = ERR_INVALID_PARAM;
        *message = "command rejected";
        return err;
    }
}

static void handle_command(const cJSON *root)
{
    const cJSON *type_item = cJSON_GetObjectItemCaseSensitive(root, "type");
    if (!cJSON_IsString(type_item)) {
        return;
    }
    const char *type = type_item->valuestring;
    int command_id = (int)json_number(root, "id", 0);

    const char *code = NULL;
    const char *message = NULL;
    bool clamped = false;

    if (strcmp(type, MSG_HELLO_ACK) == 0) {
        const cJSON *accepted = cJSON_GetObjectItemCaseSensitive(root, "accepted");
        s_accepted = cJSON_IsTrue(accepted);
        if (!s_accepted) {
            const cJSON *reason = cJSON_GetObjectItemCaseSensitive(root, "reason");
            ESP_LOGE(TAG, "server rejected this controller: %s",
                     cJSON_IsString(reason) ? reason->valuestring : "unknown");
        } else {
            ESP_LOGI(TAG, "handshake accepted");
        }
        return;
    }

    if (strcmp(type, MSG_PING) == 0) {
        cJSON *pong = new_message(MSG_PONG);
        if (pong) {
            cJSON_AddNumberToObject(pong, "id", command_id);
            cJSON_AddNumberToObject(pong, "t_ms", json_number(root, "t_ms", 0));
            send_json(pong);
        }
        return;
    }

    if (strcmp(type, MSG_STOP) == 0) {
        bool emergency = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "emergency"));
        valve_close();
        valve_set_armed(false);
        motion_stop(emergency);
        if (emergency) {
            ws_client_send_event(EVT_ESTOP, NULL, 0);
        } else if (motion_estop_active()) {
            motion_clear_estop();
            ws_client_send_event(EVT_ESTOP_CLEARED, NULL, 0);
        }
        send_ack(command_id, true, NULL, NULL, false);
        return;
    }

    if (strcmp(type, MSG_ARM_OUTPUT) == 0) {
        bool armed = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "armed"));
        if (armed && motion_estop_active()) {
            send_ack(command_id, false, ERR_ESTOP, "emergency stop is latched", false);
            return;
        }
        valve_set_armed(armed);
        send_ack(command_id, true, NULL, NULL, false);
        return;
    }

    if (strcmp(type, MSG_MOVE_ABSOLUTE) == 0) {
        esp_err_t err = motion_move_absolute(
            (float)json_number(root, "pan_deg", 0.0), (float)json_number(root, "tilt_deg", 0.0),
            (float)json_number(root, "max_speed_deg_s", 0.0),
            (float)json_number(root, "accel_deg_s2", 0.0), &clamped);
        if (motion_err_to_code(err, &code, &message) != ESP_OK) {
            send_ack(command_id, false, code, message, false);
        } else {
            send_ack(command_id, true, NULL, NULL, clamped);
        }
        return;
    }

    if (strcmp(type, MSG_MOVE_RELATIVE) == 0) {
        esp_err_t err = motion_move_relative(
            (float)json_number(root, "pan_delta_deg", 0.0),
            (float)json_number(root, "tilt_delta_deg", 0.0),
            (float)json_number(root, "max_speed_deg_s", 0.0), &clamped);
        if (motion_err_to_code(err, &code, &message) != ESP_OK) {
            send_ack(command_id, false, code, message, false);
        } else {
            send_ack(command_id, true, NULL, NULL, clamped);
        }
        return;
    }

    if (strcmp(type, MSG_JOG) == 0) {
        esp_err_t err = motion_jog((float)json_number(root, "pan_rate_deg_s", 0.0),
                                   (float)json_number(root, "tilt_rate_deg_s", 0.0),
                                   (uint32_t)json_number(root, "ttl_ms", 400));
        if (motion_err_to_code(err, &code, &message) != ESP_OK) {
            send_ack(command_id, false, code, message, false);
        } else {
            send_ack(command_id, true, NULL, NULL, false);
        }
        return;
    }

    if (strcmp(type, MSG_HOME) == 0) {
        const cJSON *axes_item = cJSON_GetObjectItemCaseSensitive(root, "axes");
        motion_axis_mask_t axes = MOTION_AXIS_BOTH;
        if (cJSON_IsString(axes_item)) {
            if (strcmp(axes_item->valuestring, "pan") == 0) {
                axes = MOTION_AXIS_PAN;
            } else if (strcmp(axes_item->valuestring, "tilt") == 0) {
                axes = MOTION_AXIS_TILT;
            }
        }
        home_request_t request = {.command_id = command_id, .axes = axes};
        /* Homing takes seconds; it is acknowledged from the worker task so
         * this handler (and therefore the WebSocket event loop) never blocks. */
        if (xQueueSend(s_home_queue, &request, 0) != pdTRUE) {
            send_ack(command_id, false, ERR_BUSY, "homing already in progress", false);
        }
        return;
    }

    if (strcmp(type, MSG_SPRAY) == 0) {
        uint32_t requested = (uint32_t)json_number(root, "duration_ms", 0);
        if (requested == 0) {
            send_ack(command_id, false, ERR_INVALID_PARAM, "duration_ms is required", false);
            return;
        }
        uint32_t applied = 0;
        esp_err_t err = valve_open(requested, &applied);
        if (err == ESP_ERR_INVALID_STATE) {
            send_ack(command_id, false, ERR_DISARMED, "output is disarmed", false);
        } else if (err != ESP_OK) {
            send_ack(command_id, false, ERR_FAULT, "valve could not be opened", false);
        } else {
            ws_client_send_event(EVT_VALVE_OPENED, "duration_ms", applied);
            send_ack(command_id, true, NULL, NULL, applied != requested);
        }
        return;
    }

    if (strcmp(type, MSG_SPRAY_STOP) == 0) {
        valve_close();
        send_ack(command_id, true, NULL, NULL, false);
        return;
    }

    if (strcmp(type, MSG_SET_CONFIG) == 0) {
        const cJSON *config = cJSON_GetObjectItemCaseSensitive(root, "config");
        int changed = turret_config_apply_json(s_config, config);
        if (changed < 0) {
            send_ack(command_id, false, ERR_INVALID_PARAM, "configuration rejected", false);
            return;
        }
        motion_apply_config(s_config);
        valve_set_max_open_ms(s_config->max_spray_ms);
        if (turret_config_save(s_config) != ESP_OK) {
            ESP_LOGW(TAG, "configuration applied but could not be persisted");
        }
        ws_client_send_event(EVT_CONFIG_SAVED, "fields", changed);
        send_ack(command_id, true, NULL, NULL, false);
        return;
    }

    if (strcmp(type, MSG_GET_CONFIG) == 0) {
        cJSON *reply = new_message(MSG_CONFIG);
        if (reply) {
            cJSON_AddNumberToObject(reply, "id", command_id);
            cJSON *config = (cJSON *)turret_config_to_json(s_config);
            if (config) {
                cJSON_AddItemToObject(reply, "config", config);
            }
            send_json(reply);
        }
        send_ack(command_id, true, NULL, NULL, false);
        return;
    }

    if (strcmp(type, MSG_REBOOT) == 0) {
        send_ack(command_id, true, NULL, NULL, false);
        valve_close();
        motion_stop(false);
        vTaskDelay(pdMS_TO_TICKS(200));
        esp_restart();
        return;
    }

    send_ack(command_id, false, ERR_UNSUPPORTED, "unknown message type", false);
}

/* ---------------------------------------------------------------------- */
/* tasks                                                                   */
/* ---------------------------------------------------------------------- */

static void home_task(void *arg)
{
    (void)arg;
    home_request_t request;
    while (true) {
        if (xQueueReceive(s_home_queue, &request, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        esp_err_t err = motion_home_start(request.axes);
        if (err != ESP_OK) {
            send_ack(request.command_id, false, ERR_BUSY, "cannot home right now", false);
            continue;
        }
        ws_client_send_event(EVT_HOMING_STARTED, NULL, 0);

        err = motion_home_wait(120000);
        if (err == ESP_OK) {
            motion_status_t status;
            motion_get_status(&status);
            ws_client_send_event(EVT_HOMING_COMPLETED, "pan_deg", status.pan_deg);
            send_ack(request.command_id, true, NULL, NULL, false);
        } else {
            ws_client_send_event(EVT_HOMING_FAILED, NULL, 0);
            send_ack(request.command_id, false, ERR_TIMEOUT, "endstop not found", false);
        }
    }
}

static void status_task(void *arg)
{
    (void)arg;
    while (true) {
        uint32_t interval = s_config->status_interval_ms;
        if (s_connected && s_accepted) {
            send_status();
        }

        /* Link failsafe. Independent of the server: if we have not heard from
         * it within the configured window, everything stops and the valve
         * closes, whatever it asked for last. */
        if (s_accepted && ws_client_ms_since_server_frame() > s_config->link_timeout_ms) {
            ESP_LOGW(TAG, "no server frame for %u ms - failsafe",
                     (unsigned)ws_client_ms_since_server_frame());
            valve_close();
            valve_set_armed(false);
            motion_stop(false);
            s_accepted = false;
            esp_websocket_client_close(s_client, pdMS_TO_TICKS(1000));
        }

        vTaskDelay(pdMS_TO_TICKS(interval < 20 ? 20 : interval));
    }
}

/* ---------------------------------------------------------------------- */
/* websocket events                                                        */
/* ---------------------------------------------------------------------- */

static void handle_payload(const char *data, size_t length)
{
    cJSON *root = cJSON_ParseWithLength(data, length);
    if (!root) {
        ESP_LOGW(TAG, "dropping unparseable frame (%u bytes)", (unsigned)length);
        return;
    }
    handle_command(root);
    cJSON_Delete(root);
}

static void websocket_event(void *arg, esp_event_base_t base, int32_t event_id, void *event_data)
{
    (void)arg;
    (void)base;
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;

    switch (event_id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "connected to %s", TURRET_SERVER_URI);
        s_connected = true;
        s_rx_length = 0;
        s_last_server_frame_us = esp_timer_get_time();
        send_hello();
        break;

    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "disconnected");
        s_connected = false;
        s_accepted = false;
        /* Losing the link is exactly the situation the failsafe exists for. */
        valve_close();
        valve_set_armed(false);
        motion_stop(false);
        break;

    case WEBSOCKET_EVENT_DATA:
        s_last_server_frame_us = esp_timer_get_time();
        if (data->op_code == 0x08) { /* close */
            ESP_LOGW(TAG, "server closed the connection");
            s_accepted = false;
            break;
        }
        if (data->op_code != 0x01 && data->op_code != 0x00) {
            break; /* not text or continuation */
        }
        if (data->payload_len > RX_BUFFER_SIZE) {
            ESP_LOGW(TAG, "frame too large (%d bytes)", data->payload_len);
            s_rx_length = 0;
            break;
        }
        /* Reassemble fragmented frames before parsing. */
        if (data->payload_offset == 0) {
            s_rx_length = 0;
        }
        if (s_rx_length + data->data_len <= RX_BUFFER_SIZE) {
            memcpy(s_rx_buffer + s_rx_length, data->data_ptr, data->data_len);
            s_rx_length += data->data_len;
        }
        if (s_rx_length >= (size_t)data->payload_len && data->payload_len > 0) {
            handle_payload(s_rx_buffer, s_rx_length);
            s_rx_length = 0;
        }
        break;

    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGW(TAG, "websocket error");
        break;

    default:
        break;
    }
}

/* ---------------------------------------------------------------------- */
/* public API                                                              */
/* ---------------------------------------------------------------------- */

bool ws_client_is_connected(void)
{
    return s_connected && s_accepted;
}

uint32_t ws_client_ms_since_server_frame(void)
{
    if (s_last_server_frame_us == 0) {
        return 0;
    }
    return (uint32_t)((esp_timer_get_time() - s_last_server_frame_us) / 1000);
}

esp_err_t ws_client_start(turret_config_t *config)
{
    s_config = config;
    s_boot_us = esp_timer_get_time();

    s_home_queue = xQueueCreate(1, sizeof(home_request_t));
    if (!s_home_queue) {
        return ESP_ERR_NO_MEM;
    }

    esp_websocket_client_config_t ws_config = {
        .uri = TURRET_SERVER_URI,
        .reconnect_timeout_ms = 2000,
        .network_timeout_ms = 5000,
        .buffer_size = RX_BUFFER_SIZE,
        .disable_auto_reconnect = false,
#if CONFIG_MBEDTLS_CERTIFICATE_BUNDLE
        /* Only consulted for wss:// URIs; a plain ws:// link on a trusted LAN
         * never touches TLS. */
        .crt_bundle_attach = esp_crt_bundle_attach,
#endif
    };

    s_client = esp_websocket_client_init(&ws_config);
    if (!s_client) {
        return ESP_FAIL;
    }
    ESP_ERROR_CHECK(esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY,
                                                  websocket_event, NULL));
    ESP_ERROR_CHECK(esp_websocket_client_start(s_client));

    xTaskCreate(status_task, "ws_status", 4096, NULL, 6, NULL);
    xTaskCreate(home_task, "ws_home", 4096, NULL, 5, NULL);

    ESP_LOGI(TAG, "client started, server=%s", TURRET_SERVER_URI);
    return ESP_OK;
}

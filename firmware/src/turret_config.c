#include "turret_config.h"

#include <math.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "config";
static const char *NVS_NAMESPACE = "turret";
static const char *NVS_KEY = "config";

/* Bumped when the struct layout changes so an old blob is discarded rather
 * than reinterpreted as garbage. */
#define CONFIG_BLOB_VERSION 1

typedef struct {
    uint32_t version;
    turret_config_t config;
} config_blob_t;

void turret_config_defaults(turret_config_t *config)
{
    memset(config, 0, sizeof(*config));
    config->steps_per_rev = 200;
    config->pan_microsteps = 16;
    config->tilt_microsteps = 16;
    config->pan_gear_ratio = 1.0f;
    config->tilt_gear_ratio = 1.0f;
    config->pan_invert = false;
    config->tilt_invert = false;

    config->pan_min_deg = -90.0f;
    config->pan_max_deg = 90.0f;
    config->tilt_min_deg = -45.0f;
    config->tilt_max_deg = 45.0f;

    config->max_speed_deg_s = 60.0f;
    config->accel_deg_s2 = 180.0f;

    config->homing_speed_deg_s = 15.0f;
    config->homing_backoff_deg = 3.0f;
    config->pan_home_dir = -1;
    config->tilt_home_dir = -1;
    config->pan_home_offset_deg = 0.0f;
    config->tilt_home_offset_deg = 0.0f;
    config->endstop_active_low = true;
    config->allow_unhomed_motion = false;

    config->max_spray_ms = 2000;
    config->link_timeout_ms = 6000;
    config->status_interval_ms = 100;
}

esp_err_t turret_config_load(turret_config_t *config)
{
    turret_config_defaults(config);

    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "no stored configuration, using defaults");
        return ESP_OK;
    }

    config_blob_t blob = {0};
    size_t length = sizeof(blob);
    err = nvs_get_blob(handle, NVS_KEY, &blob, &length);
    nvs_close(handle);

    if (err != ESP_OK || length != sizeof(blob) || blob.version != CONFIG_BLOB_VERSION) {
        ESP_LOGW(TAG, "stored configuration unusable (err=%d), using defaults", err);
        return ESP_OK;
    }

    *config = blob.config;
    ESP_LOGI(TAG, "configuration loaded from NVS");
    return ESP_OK;
}

esp_err_t turret_config_save(const turret_config_t *config)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }

    config_blob_t blob = {.version = CONFIG_BLOB_VERSION, .config = *config};
    err = nvs_set_blob(handle, NVS_KEY, &blob, sizeof(blob));
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err;
}

/* ---- JSON helpers ----------------------------------------------------- */

static bool json_float(const cJSON *root, const char *key, float min, float max, float *out)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    double value = item->valuedouble;
    if (!isfinite(value) || value < min || value > max) {
        ESP_LOGW(TAG, "rejecting %s=%.3f (out of range)", key, value);
        return false;
    }
    *out = (float)value;
    return true;
}

static bool json_u32(const cJSON *root, const char *key, uint32_t min, uint32_t max,
                     uint32_t *out)
{
    float value = 0.0f;
    if (!json_float(root, key, (float)min, (float)max, &value)) {
        return false;
    }
    *out = (uint32_t)value;
    return true;
}

static bool json_u16(const cJSON *root, const char *key, uint16_t min, uint16_t max,
                     uint16_t *out)
{
    uint32_t value = 0;
    if (!json_u32(root, key, min, max, &value)) {
        return false;
    }
    *out = (uint16_t)value;
    return true;
}

static bool json_bool(const cJSON *root, const char *key, bool *out)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
    if (!cJSON_IsBool(item)) {
        return false;
    }
    *out = cJSON_IsTrue(item);
    return true;
}

static bool json_dir(const cJSON *root, const char *key, int8_t *out)
{
    float value = 0.0f;
    if (!json_float(root, key, -1.0f, 1.0f, &value)) {
        return false;
    }
    if (value >= 0.0f) {
        *out = 1;
    } else {
        *out = -1;
    }
    return true;
}

int turret_config_apply_json(turret_config_t *config, const void *json_object)
{
    const cJSON *root = (const cJSON *)json_object;
    if (!cJSON_IsObject(root)) {
        return -1;
    }

    turret_config_t next = *config;
    int changed = 0;

    changed += json_u16(root, "steps_per_rev", 1, 10000, &next.steps_per_rev);
    changed += json_u16(root, "pan_microsteps", 1, 256, &next.pan_microsteps);
    changed += json_u16(root, "tilt_microsteps", 1, 256, &next.tilt_microsteps);
    changed += json_float(root, "pan_gear_ratio", 0.001f, 1000.0f, &next.pan_gear_ratio);
    changed += json_float(root, "tilt_gear_ratio", 0.001f, 1000.0f, &next.tilt_gear_ratio);
    changed += json_bool(root, "pan_invert", &next.pan_invert);
    changed += json_bool(root, "tilt_invert", &next.tilt_invert);

    changed += json_float(root, "pan_min_deg", -360.0f, 360.0f, &next.pan_min_deg);
    changed += json_float(root, "pan_max_deg", -360.0f, 360.0f, &next.pan_max_deg);
    changed += json_float(root, "tilt_min_deg", -360.0f, 360.0f, &next.tilt_min_deg);
    changed += json_float(root, "tilt_max_deg", -360.0f, 360.0f, &next.tilt_max_deg);

    changed += json_float(root, "max_speed_deg_s", 0.1f, 1000.0f, &next.max_speed_deg_s);
    changed += json_float(root, "accel_deg_s2", 0.1f, 10000.0f, &next.accel_deg_s2);

    changed += json_float(root, "homing_speed_deg_s", 0.1f, 200.0f, &next.homing_speed_deg_s);
    changed += json_float(root, "homing_backoff_deg", 0.1f, 45.0f, &next.homing_backoff_deg);
    changed += json_dir(root, "pan_home_dir", &next.pan_home_dir);
    changed += json_dir(root, "tilt_home_dir", &next.tilt_home_dir);
    changed += json_float(root, "pan_home_offset_deg", -360.0f, 360.0f,
                          &next.pan_home_offset_deg);
    changed += json_float(root, "tilt_home_offset_deg", -360.0f, 360.0f,
                          &next.tilt_home_offset_deg);
    changed += json_bool(root, "endstop_active_low", &next.endstop_active_low);
    changed += json_bool(root, "allow_unhomed_motion", &next.allow_unhomed_motion);

    changed += json_u32(root, "max_spray_ms", 1, 60000, &next.max_spray_ms);
    changed += json_u32(root, "link_timeout_ms", 500, 120000, &next.link_timeout_ms);
    changed += json_u32(root, "status_interval_ms", 20, 10000, &next.status_interval_ms);

    /* An inverted travel range would make homing drive the wrong way and soft
     * limits meaningless, so refuse the whole update rather than half-apply. */
    if (next.pan_min_deg >= next.pan_max_deg || next.tilt_min_deg >= next.tilt_max_deg) {
        ESP_LOGE(TAG, "rejecting configuration: travel limits are inverted");
        return -1;
    }

    *config = next;
    return changed;
}

void *turret_config_to_json(const turret_config_t *config)
{
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return NULL;
    }
    cJSON_AddNumberToObject(root, "steps_per_rev", config->steps_per_rev);
    cJSON_AddNumberToObject(root, "pan_microsteps", config->pan_microsteps);
    cJSON_AddNumberToObject(root, "tilt_microsteps", config->tilt_microsteps);
    cJSON_AddNumberToObject(root, "pan_gear_ratio", config->pan_gear_ratio);
    cJSON_AddNumberToObject(root, "tilt_gear_ratio", config->tilt_gear_ratio);
    cJSON_AddBoolToObject(root, "pan_invert", config->pan_invert);
    cJSON_AddBoolToObject(root, "tilt_invert", config->tilt_invert);
    cJSON_AddNumberToObject(root, "pan_min_deg", config->pan_min_deg);
    cJSON_AddNumberToObject(root, "pan_max_deg", config->pan_max_deg);
    cJSON_AddNumberToObject(root, "tilt_min_deg", config->tilt_min_deg);
    cJSON_AddNumberToObject(root, "tilt_max_deg", config->tilt_max_deg);
    cJSON_AddNumberToObject(root, "max_speed_deg_s", config->max_speed_deg_s);
    cJSON_AddNumberToObject(root, "accel_deg_s2", config->accel_deg_s2);
    cJSON_AddNumberToObject(root, "homing_speed_deg_s", config->homing_speed_deg_s);
    cJSON_AddNumberToObject(root, "homing_backoff_deg", config->homing_backoff_deg);
    cJSON_AddNumberToObject(root, "pan_home_dir", config->pan_home_dir);
    cJSON_AddNumberToObject(root, "tilt_home_dir", config->tilt_home_dir);
    cJSON_AddNumberToObject(root, "pan_home_offset_deg", config->pan_home_offset_deg);
    cJSON_AddNumberToObject(root, "tilt_home_offset_deg", config->tilt_home_offset_deg);
    cJSON_AddBoolToObject(root, "endstop_active_low", config->endstop_active_low);
    cJSON_AddBoolToObject(root, "allow_unhomed_motion", config->allow_unhomed_motion);
    cJSON_AddNumberToObject(root, "max_spray_ms", config->max_spray_ms);
    cJSON_AddNumberToObject(root, "link_timeout_ms", config->link_timeout_ms);
    cJSON_AddNumberToObject(root, "status_interval_ms", config->status_interval_ms);
    return root;
}

float turret_config_pan_steps_per_deg(const turret_config_t *config)
{
    return (float)config->steps_per_rev * (float)config->pan_microsteps *
           config->pan_gear_ratio / 360.0f;
}

float turret_config_tilt_steps_per_deg(const turret_config_t *config)
{
    return (float)config->steps_per_rev * (float)config->tilt_microsteps *
           config->tilt_gear_ratio / 360.0f;
}

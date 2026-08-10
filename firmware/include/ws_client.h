/*
 * WebSocket client: the controller's whole conversation with the server.
 *
 * The controller is the client, so reconnection lives here and the server
 * never has to reach out. Everything received is validated before it reaches
 * the motion or valve layers, and losing the link triggers the failsafe
 * regardless of what the server intended.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "turret_config.h"

/** Start the client and its background tasks. @p config must outlive the call. */
esp_err_t ws_client_start(turret_config_t *config);

/** True while the WebSocket is connected and the handshake has been accepted. */
bool ws_client_is_connected(void);

/** Milliseconds since the last frame from the server. */
uint32_t ws_client_ms_since_server_frame(void);

/** Send an asynchronous event (see EVT_* in protocol_generated.h). */
void ws_client_send_event(const char *event, const char *detail_key, double detail_value);

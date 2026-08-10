/*
 * Network bring-up.
 *
 * Wi-Fi station mode is what is implemented. The rest of the firmware only
 * asks one question through this interface - "is there a route to the
 * server?" - so adding Ethernet means replacing the netif setup in
 * wifi_manager.c and nothing else.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"

/** Start networking. Non-blocking: connection happens in the background. */
esp_err_t network_start(void);

/** Block until an IP address is assigned. */
esp_err_t network_wait_connected(uint32_t timeout_ms);

bool network_is_connected(void);

/** Dotted-quad address, or "0.0.0.0" while disconnected. */
const char *network_ip_address(void);

/** Interface MAC as "aa:bb:cc:dd:ee:ff" - used as the controller's identity. */
const char *network_mac_address(void);

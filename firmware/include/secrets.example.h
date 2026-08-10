/*
 * Copy to include/secrets.h and fill in. secrets.h is git-ignored.
 *
 * Everything here can also be supplied as a build flag in platformio.ini,
 * which is what CI does; values defined there win over this file.
 */

#pragma once

#ifndef TURRET_WIFI_SSID
#define TURRET_WIFI_SSID "my-network"
#endif

#ifndef TURRET_WIFI_PASSWORD
#define TURRET_WIFI_PASSWORD "my-password"
#endif

/* WebSocket endpoint of the control server. */
#ifndef TURRET_SERVER_URI
#define TURRET_SERVER_URI "ws://192.168.1.10:8080/ws/hardware"
#endif

/* Must match TURRET_CONTROLLER_TOKEN in the server's environment file.
 * Leave empty only on a network you fully trust. */
#ifndef TURRET_CONTROLLER_TOKEN
#define TURRET_CONTROLLER_TOKEN ""
#endif

#ifndef TURRET_CONTROLLER_ID
#define TURRET_CONTROLLER_ID "turret-1"
#endif

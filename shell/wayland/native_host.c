#define _GNU_SOURCE
#include <wayland-server-core.h>
#include <wayland-server-protocol.h>
#include "xdg-shell-server-protocol.h"
#include "xdg-decoration-server-protocol.h"
#include "text-input-v3-server-protocol.h"
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>

enum nx_event_type { NX_TOPLEVEL = 1, NX_TITLE = 2, NX_FRAME = 3, NX_UNMAP = 4,
                     NX_DESTROY = 5, NX_POPUP = 6, NX_TEXT_INPUT = 7 };
enum nx_surface_role { NX_ROLE_NONE, NX_ROLE_TOPLEVEL, NX_ROLE_POPUP, NX_ROLE_CURSOR };

struct nx_event {
    int type;
    uint64_t surface;
    uint64_t parent;
    int x, y;
    int width, height, stride, format;
    char app_id[256];
    char title[512];
    void *pixels;
    struct nx_event *next;
};

struct nx_server;
static void xdg_surface_resource_destroy(struct wl_resource *resource);
static void xdg_toplevel_resource_destroy(struct wl_resource *resource);
static void xdg_popup_resource_destroy(struct wl_resource *resource);
static const struct xdg_popup_interface xdg_popup_impl;
static const struct zwp_text_input_v3_interface text_input_impl;
static const struct zwp_text_input_manager_v3_interface text_input_manager_impl;

struct nx_surface {
    struct wl_list link;
    struct nx_server *server;
    uint64_t id;
    struct wl_resource *resource;
    struct wl_resource *xdg_surface;
    struct wl_resource *toplevel;
    struct wl_resource *popup;
    struct wl_resource *decoration;
    struct nx_surface *popup_parent;
    int popup_x, popup_y;
    int popup_width, popup_height;
    struct wl_resource *pending_buffer;
    int pending_attached;
    struct wl_list callbacks;
    char app_id[256];
    char title[512];
    int configured;
    int mapped;
    enum nx_surface_role role;
};

struct nx_positioner {
    int width, height;
    int rect_x, rect_y, rect_width, rect_height;
    int anchor, gravity;
    int offset_x, offset_y;
};

struct nx_callback {
    struct wl_list link;
    struct wl_resource *resource;
};

struct nx_pointer {
    struct wl_list link;
    struct nx_server *server;
    struct wl_client *client;
    struct wl_resource *resource;
    struct nx_surface *focus;
};

struct nx_keyboard {
    struct wl_list link;
    struct nx_server *server;
    struct wl_client *client;
    struct wl_resource *resource;
    struct nx_surface *focus;
    uint32_t depressed;
    uint32_t locked;
    int caps_down;
    int num_down;
};

struct nx_output {
    struct wl_list link;
    struct wl_resource *resource;
};

struct nx_text_input {
    struct wl_list link;
    struct nx_server *server;
    struct wl_client *client;
    struct wl_resource *resource;
    struct nx_surface *surface;
    int enabled;
    int pending_change;
    int pending_enabled;
};

struct nx_server {
    struct wl_display *display;
    struct wl_global *compositor;
    struct wl_global *shell;
    struct wl_global *decoration_manager;
    struct wl_global *output;
    struct wl_global *seat;
    struct wl_global *text_input_manager;
    struct wl_list surfaces;
    struct wl_list pointers;
    struct wl_list keyboards;
    struct wl_list outputs;
    struct wl_list text_inputs;
    uint64_t next_surface;
    uint32_t text_input_serial;
    int output_width, output_height;
    int wake_read, wake_write;
    int keymap_fd;
    struct wl_event_source *wake_source;
    pthread_mutex_t event_lock;
    pthread_mutex_t api_lock;
    struct nx_event *events_head, *events_tail;
};

static int wake_dispatch(int fd, uint32_t mask, void *data) {
    (void)mask; (void)data;
    char bytes[128];
    while (read(fd, bytes, sizeof(bytes)) > 0) {}
    return 0;
}

static void wake_server(struct nx_server *server) {
    if (!server || server->wake_write < 0) return;
    const char byte = 1;
    (void)write(server->wake_write, &byte, 1);
}

static void queue_event(struct nx_surface *surface, int type, const void *pixels,
                        int width, int height, int stride, int format) {
    struct nx_event *event = calloc(1, sizeof(*event));
    if (!event) return;
    event->type = type;
    event->surface = surface->id;
    event->parent = surface->popup_parent ? surface->popup_parent->id : 0;
    event->x = surface->popup_x;
    event->y = surface->popup_y;
    event->width = width;
    event->height = height;
    event->stride = stride;
    event->format = format;
    memcpy(event->app_id, surface->app_id, sizeof(event->app_id));
    memcpy(event->title, surface->title, sizeof(event->title));
    if (pixels && width > 0 && height > 0 && stride >= width * 4) {
        size_t size = (size_t)stride * (size_t)height;
        event->pixels = malloc(size);
        if (event->pixels) memcpy(event->pixels, pixels, size);
        if (event->pixels && format == WL_SHM_FORMAT_XRGB8888) {
            unsigned char *row = event->pixels;
            for (int y = 0; y < height; y++, row += stride)
                for (int x = 3; x < width * 4; x += 4) row[x] = 0xff;
            event->format = WL_SHM_FORMAT_ARGB8888;
        }
    }
    pthread_mutex_lock(&surface->server->event_lock);
    if (type == NX_FRAME) {
        struct nx_event **cursor = &surface->server->events_head;
        while (*cursor) {
            struct nx_event *old = *cursor;
            if (old->type == NX_FRAME && old->surface == surface->id) {
                *cursor = old->next;
                if (surface->server->events_tail == old)
                    surface->server->events_tail = NULL;
                free(old->pixels);
                free(old);
                continue;
            }
            cursor = &old->next;
        }
        if (!surface->server->events_head) surface->server->events_tail = NULL;
        else if (!surface->server->events_tail) {
            struct nx_event *tail = surface->server->events_head;
            while (tail->next) tail = tail->next;
            surface->server->events_tail = tail;
        }
    }
    if (surface->server->events_tail) surface->server->events_tail->next = event;
    else surface->server->events_head = event;
    surface->server->events_tail = event;
    pthread_mutex_unlock(&surface->server->event_lock);
}

static void surface_resource_destroy(struct wl_resource *resource) {
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    if (!surface) return;
    struct nx_surface *child;
    wl_list_for_each(child, &surface->server->surfaces, link) {
        if (child->popup_parent != surface) continue;
        if (child->popup) xdg_popup_send_popup_done(child->popup);
        child->popup_parent = NULL;
        if (child->mapped) {
            child->mapped = 0;
            queue_event(child, NX_UNMAP, NULL, 0, 0, 0, 0);
        }
    }
    struct nx_pointer *pointer;
    uint32_t serial = wl_display_next_serial(surface->server->display);
    wl_list_for_each(pointer, &surface->server->pointers, link) {
        if (pointer->focus == surface) {
            wl_pointer_send_leave(pointer->resource, serial, surface->resource);
            pointer->focus = NULL;
        }
    }
    struct nx_keyboard *keyboard;
    wl_list_for_each(keyboard, &surface->server->keyboards, link) {
        if (keyboard->focus == surface) {
            wl_keyboard_send_leave(keyboard->resource, serial, surface->resource);
            keyboard->focus = NULL;
        }
    }
    struct nx_text_input *input;
    wl_list_for_each(input, &surface->server->text_inputs, link) {
        if (input->surface != surface) continue;
        if (input->enabled)
            queue_event(surface, NX_TEXT_INPUT, NULL, 0, 0, 0, 0);
        zwp_text_input_v3_send_leave(input->resource, surface->resource);
        input->surface = NULL;
        input->enabled = 0;
    }
    struct nx_callback *callback, *next;
    wl_list_for_each_safe(callback, next, &surface->callbacks, link) {
        wl_resource_destroy(callback->resource);
        wl_list_remove(&callback->link);
        free(callback);
    }
    if (surface->popup) wl_resource_destroy(surface->popup);
    if (surface->decoration) wl_resource_destroy(surface->decoration);
    if (surface->toplevel) wl_resource_destroy(surface->toplevel);
    if (surface->xdg_surface) wl_resource_destroy(surface->xdg_surface);
    if (surface->mapped) queue_event(surface, NX_UNMAP, NULL, 0, 0, 0, 0);
    queue_event(surface, NX_DESTROY, NULL, 0, 0, 0, 0);
    wl_list_remove(&surface->link);
    free(surface);
}

static void surface_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static void surface_attach(struct wl_client *client, struct wl_resource *resource,
                           struct wl_resource *buffer, int32_t x, int32_t y) {
    (void)client; (void)x; (void)y;
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    surface->pending_buffer = buffer;
    surface->pending_attached = 1;
}

static void surface_frame(struct wl_client *client, struct wl_resource *resource, uint32_t id) {
    struct wl_resource *callback = wl_resource_create(client, &wl_callback_interface, 1, id);
    if (!callback) return;
    wl_resource_set_implementation(callback, NULL, NULL, NULL);
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    struct nx_callback *pending = calloc(1, sizeof(*pending));
    if (!pending) { wl_resource_destroy(callback); return; }
    pending->resource = callback;
    wl_list_insert(surface->callbacks.prev, &pending->link);
}

static void surface_region(struct wl_client *client, struct wl_resource *resource,
                           struct wl_resource *region) {
    (void)client; (void)resource; (void)region;
}
static void surface_damage(struct wl_client *client, struct wl_resource *resource,
                           int32_t x, int32_t y, int32_t width, int32_t height) {
    (void)client; (void)resource; (void)x; (void)y; (void)width; (void)height;
}
static void surface_transform(struct wl_client *client, struct wl_resource *resource,
                              int32_t transform) {
    (void)client; (void)resource; (void)transform;
}
static void surface_scale(struct wl_client *client, struct wl_resource *resource, int32_t scale) {
    (void)client; (void)resource; (void)scale;
}
static void surface_offset(struct wl_client *client, struct wl_resource *resource,
                           int32_t x, int32_t y) {
    (void)client; (void)resource; (void)x; (void)y;
}
static void surface_get_release(struct wl_client *client, struct wl_resource *resource, uint32_t id) {
    struct wl_resource *callback = wl_resource_create(client, &wl_callback_interface, 1, id);
    if (!callback) return;
    wl_resource_set_implementation(callback, NULL, NULL, NULL);
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    struct nx_callback *pending = calloc(1, sizeof(*pending));
    if (!pending) { wl_resource_destroy(callback); return; }
    pending->resource = callback;
    wl_list_insert(surface->callbacks.prev, &pending->link);
}
static void complete_callbacks(struct nx_surface *surface) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint32_t ms = (uint32_t)(now.tv_sec * 1000 + now.tv_nsec / 1000000);
    struct nx_callback *callback, *next;
    wl_list_for_each_safe(callback, next, &surface->callbacks, link) {
        wl_callback_send_done(callback->resource, ms);
        wl_resource_destroy(callback->resource);
        wl_list_remove(&callback->link);
        free(callback);
    }
}

static void surface_commit(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    if (!surface->pending_attached) {
        complete_callbacks(surface);
        return;
    }
    surface->pending_attached = 0;
    struct wl_resource *buffer_resource = surface->pending_buffer;
    surface->pending_buffer = NULL;
    if (!buffer_resource) {
        if (surface->mapped) {
            surface->mapped = 0;
            queue_event(surface, NX_UNMAP, NULL, 0, 0, 0, 0);
        }
        complete_callbacks(surface);
        return;
    }
    struct wl_shm_buffer *buffer = wl_shm_buffer_get(buffer_resource);
    if (!buffer) return;
    wl_shm_buffer_begin_access(buffer);
    int width = wl_shm_buffer_get_width(buffer);
    int height = wl_shm_buffer_get_height(buffer);
    int stride = wl_shm_buffer_get_stride(buffer);
    int format = wl_shm_buffer_get_format(buffer);
    void *pixels = wl_shm_buffer_get_data(buffer);
    if (surface->role != NX_ROLE_NONE && surface->role != NX_ROLE_CURSOR
        && width > 0 && height > 0 && stride > 0
        && (size_t)width <= (size_t)stride / 4
        && (size_t)stride * (size_t)height <= 64u * 1024u * 1024u) {
        if (!surface->mapped)
            queue_event(surface, surface->popup_parent ? NX_POPUP : NX_TOPLEVEL,
                        NULL, surface->popup_width, surface->popup_height, 0, 0);
        surface->mapped = 1;
        queue_event(surface, NX_FRAME, pixels, width, height, stride, format);
    }
    wl_shm_buffer_end_access(buffer);
    wl_buffer_send_release(buffer_resource);
    complete_callbacks(surface);
}

static const struct wl_surface_interface surface_impl = {
    .destroy = surface_destroy,
    .attach = surface_attach,
    .damage = surface_damage,
    .frame = surface_frame,
    .set_opaque_region = surface_region,
    .set_input_region = surface_region,
    .commit = surface_commit,
    .set_buffer_transform = surface_transform,
    .set_buffer_scale = surface_scale,
    .damage_buffer = surface_damage,
    .offset = surface_offset,
    .get_release = surface_get_release,
};

static const struct wl_region_interface region_impl = {
    .destroy = surface_destroy,
    .add = NULL,
    .subtract = NULL,
};

static void compositor_create_surface(struct wl_client *client, struct wl_resource *resource,
                                      uint32_t id) {
    struct nx_server *server = wl_resource_get_user_data(resource);
    struct nx_surface *surface = calloc(1, sizeof(*surface));
    if (!surface) return;
    surface->server = server;
    surface->id = ++server->next_surface;
    wl_list_init(&surface->callbacks);
    strcpy(surface->app_id, "wayland-app");
    strcpy(surface->title, "Wayland app");
    surface->resource = wl_resource_create(client, &wl_surface_interface,
                                            wl_resource_get_version(resource), id);
    if (!surface->resource) { free(surface); return; }
    wl_resource_set_implementation(surface->resource, &surface_impl, surface,
                                   surface_resource_destroy);
    wl_list_insert(&server->surfaces, &surface->link);
}

static void compositor_create_region(struct wl_client *client, struct wl_resource *resource,
                                     uint32_t id) {
    struct wl_resource *region = wl_resource_create(client, &wl_region_interface,
                                                     wl_resource_get_version(resource), id);
    if (!region) return;
    wl_resource_set_implementation(region, &region_impl, NULL, NULL);
}

static const struct wl_compositor_interface compositor_impl = {
    .create_surface = compositor_create_surface,
    .create_region = compositor_create_region,
};

static void output_release(struct wl_client *client, struct wl_resource *resource) {
    (void)client; wl_resource_destroy(resource);
}
static const struct wl_output_interface output_impl = { .release = output_release };
static void output_destroy(struct wl_resource *resource) {
    struct nx_output *output = wl_resource_get_user_data(resource);
    if (!output) return;
    wl_list_remove(&output->link);
    free(output);
}

static void bind_output(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    struct nx_server *server = data;
    struct wl_resource *resource = wl_resource_create(client, &wl_output_interface,
                                                       version > 4 ? 4 : version, id);
    if (!resource) return;
    struct nx_output *output = calloc(1, sizeof(*output));
    if (!output) { wl_resource_destroy(resource); return; }
    output->resource = resource;
    wl_resource_set_implementation(resource, &output_impl, output, output_destroy);
    wl_list_insert(&server->outputs, &output->link);
    wl_output_send_geometry(resource, 0, 0, 0, 0, WL_OUTPUT_SUBPIXEL_UNKNOWN,
                            "Nexa", "Nexa display", WL_OUTPUT_TRANSFORM_NORMAL);
    wl_output_send_mode(resource, WL_OUTPUT_MODE_CURRENT, server->output_width,
                        server->output_height, 60000);
    if (wl_resource_get_version(resource) >= 2) wl_output_send_scale(resource, 1);
    if (wl_resource_get_version(resource) >= 2) wl_output_send_done(resource);
    if (wl_resource_get_version(resource) >= 4) {
        wl_output_send_name(resource, "nexa-0");
        wl_output_send_description(resource, "Nexa virtual display");
    }
}

static void pointer_set_cursor(struct wl_client *client, struct wl_resource *resource,
                               uint32_t serial, struct wl_resource *surface,
                               int32_t x, int32_t y) {
    (void)serial; (void)x; (void)y;
    if (!surface) return;
    if (wl_resource_get_client(surface) != client) {
        wl_resource_post_error(resource, WL_POINTER_ERROR_ROLE,
                               "cursor surface belongs to another client");
        return;
    }
    struct nx_surface *cursor = wl_resource_get_user_data(surface);
    if (cursor->role != NX_ROLE_NONE && cursor->role != NX_ROLE_CURSOR) {
        wl_resource_post_error(resource, WL_POINTER_ERROR_ROLE,
                               "surface already has a different role");
        return;
    }
    cursor->role = NX_ROLE_CURSOR;
}
static void pointer_release(struct wl_client *client, struct wl_resource *resource) {
    (void)client; wl_resource_destroy(resource);
}
static const struct wl_pointer_interface pointer_impl = {
    .set_cursor = pointer_set_cursor,
    .release = pointer_release,
};
static void pointer_destroy(struct wl_resource *resource) {
    struct nx_pointer *pointer = wl_resource_get_user_data(resource);
    if (!pointer) return;
    wl_list_remove(&pointer->link);
    free(pointer);
}
static void keyboard_release(struct wl_client *client, struct wl_resource *resource) {
    (void)client; wl_resource_destroy(resource);
}
static const struct wl_keyboard_interface keyboard_impl = { .release = keyboard_release };
static void keyboard_destroy(struct wl_resource *resource) {
    struct nx_keyboard *keyboard = wl_resource_get_user_data(resource);
    if (!keyboard) return;
    wl_list_remove(&keyboard->link);
    free(keyboard);
}
static void seat_get_pointer(struct wl_client *client, struct wl_resource *seat, uint32_t id) {
    struct nx_server *server = wl_resource_get_user_data(seat);
    struct nx_pointer *pointer = calloc(1, sizeof(*pointer));
    if (!pointer) return;
    pointer->server = server;
    pointer->client = client;
    pointer->resource = wl_resource_create(client, &wl_pointer_interface,
                                            wl_resource_get_version(seat), id);
    if (!pointer->resource) { free(pointer); return; }
    wl_resource_set_implementation(pointer->resource, &pointer_impl, pointer, pointer_destroy);
    wl_list_insert(&server->pointers, &pointer->link);
}
static void seat_get_keyboard(struct wl_client *client, struct wl_resource *seat, uint32_t id) {
    struct nx_server *server = wl_resource_get_user_data(seat);
    struct nx_keyboard *keyboard = calloc(1, sizeof(*keyboard));
    if (!keyboard) return;
    keyboard->server = server;
    keyboard->client = client;
    keyboard->resource = wl_resource_create(client, &wl_keyboard_interface,
                                             wl_resource_get_version(seat), id);
    if (!keyboard->resource) { free(keyboard); return; }
    wl_resource_set_implementation(keyboard->resource, &keyboard_impl, keyboard, keyboard_destroy);
    if (server->keymap_fd >= 0) {
        int fd = dup(server->keymap_fd);
        if (fd >= 0)
            wl_keyboard_send_keymap(keyboard->resource, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1,
                                    fd, (uint32_t)lseek(server->keymap_fd, 0, SEEK_END));
        else
            wl_keyboard_send_keymap(keyboard->resource, WL_KEYBOARD_KEYMAP_FORMAT_NO_KEYMAP,
                                    -1, 0);
    } else {
        wl_keyboard_send_keymap(keyboard->resource, WL_KEYBOARD_KEYMAP_FORMAT_NO_KEYMAP,
                                -1, 0);
    }
    if (wl_resource_get_version(keyboard->resource) >= 4)
        wl_keyboard_send_repeat_info(keyboard->resource, 25, 600);
    wl_list_insert(&server->keyboards, &keyboard->link);
    struct nx_keyboard *other;
    wl_list_for_each(other, &server->keyboards, link) {
        if (other == keyboard || other->client != client || !other->focus) continue;
        keyboard->focus = other->focus;
        struct wl_array keys;
        wl_array_init(&keys);
        wl_keyboard_send_enter(keyboard->resource,
            wl_display_next_serial(server->display), keyboard->focus->resource, &keys);
        wl_array_release(&keys);
        break;
    }
}
static void seat_release(struct wl_client *client, struct wl_resource *resource) {
    (void)client; wl_resource_destroy(resource);
}
static const struct wl_seat_interface seat_impl = {
    .get_pointer = seat_get_pointer,
    .get_keyboard = seat_get_keyboard,
    .get_touch = NULL,
    .release = seat_release,
};
static void bind_seat(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    struct wl_resource *resource = wl_resource_create(client, &wl_seat_interface,
                                                       version > 7 ? 7 : version, id);
    if (!resource) return;
    wl_resource_set_implementation(resource, &seat_impl, data, NULL);
    wl_seat_send_capabilities(resource, WL_SEAT_CAPABILITY_POINTER | WL_SEAT_CAPABILITY_KEYBOARD);
    if (wl_resource_get_version(resource) >= 2) wl_seat_send_name(resource, "nexa-seat");
}

static void text_input_resource_destroy(struct wl_resource *resource) {
    struct nx_text_input *input = wl_resource_get_user_data(resource);
    if (!input) return;
    if (input->enabled && input->surface)
        queue_event(input->surface, NX_TEXT_INPUT, NULL, 0, 0, 0, 0);
    wl_list_remove(&input->link);
    free(input);
}

static void text_input_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static void text_input_enable(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    struct nx_text_input *input = wl_resource_get_user_data(resource);
    input->pending_change = 1;
    input->pending_enabled = 1;
}

static void text_input_disable(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    struct nx_text_input *input = wl_resource_get_user_data(resource);
    input->pending_change = 1;
    input->pending_enabled = 0;
}

static void text_input_surrounding_text(struct wl_client *client, struct wl_resource *resource,
        const char *text, int32_t cursor, int32_t anchor) {
    (void)client; (void)resource; (void)text; (void)cursor; (void)anchor;
}

static void text_input_change_cause(struct wl_client *client, struct wl_resource *resource,
                                    uint32_t cause) {
    (void)client; (void)resource; (void)cause;
}

static void text_input_content_type(struct wl_client *client, struct wl_resource *resource,
                                    uint32_t hint, uint32_t purpose) {
    (void)client; (void)resource; (void)hint; (void)purpose;
}

static void text_input_cursor_rectangle(struct wl_client *client, struct wl_resource *resource,
        int32_t x, int32_t y, int32_t width, int32_t height) {
    (void)client; (void)resource; (void)x; (void)y; (void)width; (void)height;
}

static void text_input_commit(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    struct nx_text_input *input = wl_resource_get_user_data(resource);
    if (!input->pending_change) return;
    if (input->pending_enabled && input->surface) {
        struct nx_text_input *other;
        wl_list_for_each(other, &input->server->text_inputs, link) {
            if (other != input && other->enabled && other->surface) {
                input->pending_change = 0;
                return;
            }
        }
    }
    input->enabled = input->pending_enabled;
    input->pending_change = 0;
    if (input->surface)
        queue_event(input->surface, NX_TEXT_INPUT, NULL, input->enabled, 0, 0, 0);
}

static const struct zwp_text_input_v3_interface text_input_impl = {
    .destroy = text_input_destroy,
    .enable = text_input_enable,
    .disable = text_input_disable,
    .set_surrounding_text = text_input_surrounding_text,
    .set_text_change_cause = text_input_change_cause,
    .set_content_type = text_input_content_type,
    .set_cursor_rectangle = text_input_cursor_rectangle,
    .commit = text_input_commit,
};

static void text_input_manager_destroy(struct wl_client *client,
                                      struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static void text_input_manager_get_text_input(struct wl_client *client,
        struct wl_resource *resource, uint32_t id, struct wl_resource *seat) {
    struct nx_server *server = wl_resource_get_user_data(resource);
    if (!seat || wl_resource_get_user_data(seat) != server
        || wl_resource_get_client(seat) != client) {
        wl_resource_post_error(resource, 0, "text input seat is not owned by this client");
        return;
    }
    struct nx_text_input *input = calloc(1, sizeof(*input));
    if (!input) return;
    input->server = server;
    input->client = client;
    input->resource = wl_resource_create(client, &zwp_text_input_v3_interface, 1, id);
    if (!input->resource) { free(input); return; }
    wl_resource_set_implementation(input->resource, &text_input_impl, input,
                                   text_input_resource_destroy);
    wl_list_insert(&server->text_inputs, &input->link);
    struct nx_keyboard *keyboard;
    wl_list_for_each(keyboard, &server->keyboards, link) {
        if (keyboard->client != client || !keyboard->focus) continue;
        input->surface = keyboard->focus;
        zwp_text_input_v3_send_enter(input->resource, input->surface->resource);
        break;
    }
}

static const struct zwp_text_input_manager_v3_interface text_input_manager_impl = {
    .destroy = text_input_manager_destroy,
    .get_text_input = text_input_manager_get_text_input,
};

static void bind_text_input_manager(struct wl_client *client, void *data,
                                    uint32_t version, uint32_t id) {
    struct wl_resource *resource = wl_resource_create(
        client, &zwp_text_input_manager_v3_interface, version > 1 ? 1 : version, id);
    if (resource)
        wl_resource_set_implementation(resource, &text_input_manager_impl, data, NULL);
}

static void bind_compositor(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    struct nx_server *server = data;
    struct wl_resource *resource = wl_resource_create(client, &wl_compositor_interface,
                                                       version > 5 ? 5 : version, id);
    if (resource) wl_resource_set_implementation(resource, &compositor_impl, server, NULL);
}

static void xdg_wm_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client; wl_resource_destroy(resource);
}
static void positioner_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}
static void positioner_resource_destroy(struct wl_resource *resource) {
    free(wl_resource_get_user_data(resource));
}
static struct nx_positioner *positioner_data(struct wl_resource *resource) {
    return wl_resource_get_user_data(resource);
}
static void positioner_set_size(struct wl_client *client, struct wl_resource *resource,
                                int32_t width, int32_t height) {
    (void)client;
    if (width <= 0 || height <= 0) {
        wl_resource_post_error(resource, XDG_POSITIONER_ERROR_INVALID_INPUT,
                               "popup size must be positive");
        return;
    }
    struct nx_positioner *positioner = positioner_data(resource);
    positioner->width = width;
    positioner->height = height;
}
static void positioner_set_anchor_rect(struct wl_client *client, struct wl_resource *resource,
                                       int32_t x, int32_t y, int32_t width, int32_t height) {
    (void)client;
    if (width < 0 || height < 0) {
        wl_resource_post_error(resource, XDG_POSITIONER_ERROR_INVALID_INPUT,
                               "anchor rectangle size cannot be negative");
        return;
    }
    struct nx_positioner *positioner = positioner_data(resource);
    positioner->rect_x = x;
    positioner->rect_y = y;
    positioner->rect_width = width;
    positioner->rect_height = height;
}
static void positioner_set_anchor(struct wl_client *client, struct wl_resource *resource,
                                  uint32_t anchor) {
    (void)client;
    if (!xdg_positioner_anchor_is_valid(anchor, wl_resource_get_version(resource))) {
        wl_resource_post_error(resource, XDG_POSITIONER_ERROR_INVALID_INPUT,
                               "invalid popup anchor");
        return;
    }
    struct nx_positioner *positioner = positioner_data(resource);
    positioner->anchor = (int)anchor;
}
static void positioner_set_gravity(struct wl_client *client, struct wl_resource *resource,
                                   uint32_t gravity) {
    (void)client;
    if (!xdg_positioner_gravity_is_valid(gravity, wl_resource_get_version(resource))) {
        wl_resource_post_error(resource, XDG_POSITIONER_ERROR_INVALID_INPUT,
                               "invalid popup gravity");
        return;
    }
    struct nx_positioner *positioner = positioner_data(resource);
    positioner->gravity = (int)gravity;
}
static void positioner_set_adjustment(struct wl_client *client, struct wl_resource *resource,
                                      uint32_t adjustment) {
    (void)client;
    if (!xdg_positioner_constraint_adjustment_is_valid(
            adjustment, wl_resource_get_version(resource))) {
        wl_resource_post_error(resource, XDG_POSITIONER_ERROR_INVALID_INPUT,
                               "invalid popup constraint adjustment");
    }
}
static void positioner_set_offset(struct wl_client *client, struct wl_resource *resource,
                                  int32_t x, int32_t y) {
    (void)client;
    struct nx_positioner *positioner = positioner_data(resource);
    positioner->offset_x = x;
    positioner->offset_y = y;
}
static void positioner_noop(struct wl_client *client, struct wl_resource *resource) {
    (void)client; (void)resource;
}
static void positioner_size_noop(struct wl_client *client, struct wl_resource *resource,
                                 int32_t width, int32_t height) {
    (void)client; (void)resource; (void)width; (void)height;
}
static void positioner_configure_noop(struct wl_client *client, struct wl_resource *resource,
                                      uint32_t serial) {
    (void)client; (void)resource; (void)serial;
}
static const struct xdg_positioner_interface positioner_impl = {
    .destroy = positioner_destroy,
    .set_size = positioner_set_size,
    .set_anchor_rect = positioner_set_anchor_rect,
    .set_anchor = positioner_set_anchor,
    .set_gravity = positioner_set_gravity,
    .set_constraint_adjustment = positioner_set_adjustment,
    .set_offset = positioner_set_offset,
    .set_reactive = positioner_noop,
    .set_parent_size = positioner_size_noop,
    .set_parent_configure = positioner_configure_noop,
};
static void popup_position(struct nx_surface *surface, struct nx_positioner *positioner,
                           int *x, int *y) {
    int anchor_x = positioner->rect_x;
    int anchor_y = positioner->rect_y;
    int anchor = positioner->anchor;
    if (anchor == XDG_POSITIONER_ANCHOR_TOP || anchor == XDG_POSITIONER_ANCHOR_BOTTOM)
        anchor_x += positioner->rect_width / 2;
    else if (anchor == XDG_POSITIONER_ANCHOR_LEFT || anchor == XDG_POSITIONER_ANCHOR_RIGHT)
        anchor_y += positioner->rect_height / 2;
    else if (anchor == XDG_POSITIONER_ANCHOR_NONE) {
        anchor_x += positioner->rect_width / 2;
        anchor_y += positioner->rect_height / 2;
    } else {
        if (anchor == XDG_POSITIONER_ANCHOR_TOP_RIGHT
            || anchor == XDG_POSITIONER_ANCHOR_BOTTOM_RIGHT)
            anchor_x += positioner->rect_width;
        if (anchor == XDG_POSITIONER_ANCHOR_BOTTOM_LEFT
            || anchor == XDG_POSITIONER_ANCHOR_BOTTOM_RIGHT)
            anchor_y += positioner->rect_height;
    }

    int gravity = positioner->gravity;
    int gx = 0, gy = 0;
    if (gravity == XDG_POSITIONER_GRAVITY_LEFT
        || gravity == XDG_POSITIONER_GRAVITY_TOP_LEFT
        || gravity == XDG_POSITIONER_GRAVITY_BOTTOM_LEFT) gx = -1;
    if (gravity == XDG_POSITIONER_GRAVITY_RIGHT
        || gravity == XDG_POSITIONER_GRAVITY_TOP_RIGHT
        || gravity == XDG_POSITIONER_GRAVITY_BOTTOM_RIGHT) gx = 1;
    if (gravity == XDG_POSITIONER_GRAVITY_TOP
        || gravity == XDG_POSITIONER_GRAVITY_TOP_LEFT
        || gravity == XDG_POSITIONER_GRAVITY_TOP_RIGHT) gy = -1;
    if (gravity == XDG_POSITIONER_GRAVITY_BOTTOM
        || gravity == XDG_POSITIONER_GRAVITY_BOTTOM_LEFT
        || gravity == XDG_POSITIONER_GRAVITY_BOTTOM_RIGHT) gy = 1;

    *x = anchor_x + positioner->offset_x - (gx < 0 ? positioner->width
        : gx == 0 ? positioner->width / 2 : 0);
    *y = anchor_y + positioner->offset_y - (gy < 0 ? positioner->height
        : gy == 0 ? positioner->height / 2 : 0);
    if (*x < 0) *x = 0;
    if (*y < 0) *y = 0;
    if (surface->popup_parent) {
        int max_x = surface->server->output_width - positioner->width;
        int max_y = surface->server->output_height - positioner->height;
        if (max_x >= 0 && *x > max_x) *x = max_x;
        if (max_y >= 0 && *y > max_y) *y = max_y;
    }
}
static void popup_configure(struct nx_surface *surface, struct nx_positioner *positioner) {
    int x, y;
    popup_position(surface, positioner, &x, &y);
    surface->popup_x = (surface->popup_parent ? surface->popup_parent->popup_x : 0) + x;
    surface->popup_y = (surface->popup_parent ? surface->popup_parent->popup_y : 0) + y;
    surface->popup_width = positioner->width;
    surface->popup_height = positioner->height;
    xdg_popup_send_configure(surface->popup, x, y, positioner->width, positioner->height);
    xdg_surface_send_configure(surface->xdg_surface,
                               wl_display_next_serial(surface->server->display));
    if (surface->mapped)
        queue_event(surface, NX_POPUP, NULL, positioner->width, positioner->height, 0, 0);
}
static void xdg_popup_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}
static void xdg_popup_resource_destroy(struct wl_resource *resource) {
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    if (!surface) return;
    if (surface->mapped) {
        surface->mapped = 0;
        queue_event(surface, NX_UNMAP, NULL, 0, 0, 0, 0);
    }
    if (surface->popup == resource) surface->popup = NULL;
}
static void xdg_popup_grab(struct wl_client *client, struct wl_resource *resource,
                           struct wl_resource *seat, uint32_t serial) {
    (void)client; (void)resource; (void)seat; (void)serial;
}
static void xdg_popup_reposition(struct wl_client *client, struct wl_resource *resource,
                                 struct wl_resource *positioner, uint32_t token) {
    (void)client;
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    struct nx_positioner *position = positioner_data(positioner);
    if (!surface || !position || position->width <= 0 || position->height <= 0) return;
    if (wl_resource_get_version(resource) >= 3)
        xdg_popup_send_repositioned(resource, token);
    popup_configure(surface, position);
}
static const struct xdg_popup_interface xdg_popup_impl = {
    .destroy = xdg_popup_destroy,
    .grab = xdg_popup_grab,
    .reposition = xdg_popup_reposition,
};
static void xdg_wm_positioner(struct wl_client *client, struct wl_resource *resource, uint32_t id) {
    (void)resource;
    struct wl_resource *positioner = wl_resource_create(client, &xdg_positioner_interface, 1, id);
    if (!positioner) return;
    struct nx_positioner *data = calloc(1, sizeof(*data));
    if (!data) { wl_resource_destroy(positioner); return; }
    wl_resource_set_implementation(positioner, &positioner_impl, data,
                                   positioner_resource_destroy);
}
static void xdg_wm_get_surface(struct wl_client *client, struct wl_resource *resource,
                               uint32_t id, struct wl_resource *surface_resource) {
    struct nx_surface *surface = wl_resource_get_user_data(surface_resource);
    if (!surface || surface->xdg_surface) {
        wl_resource_post_error(resource, XDG_WM_BASE_ERROR_ROLE,
                               "surface already has an xdg role");
        return;
    }
    surface->xdg_surface = wl_resource_create(client, &xdg_surface_interface,
                                               wl_resource_get_version(resource), id);
    if (!surface->xdg_surface) return;
    wl_resource_set_user_data(surface->xdg_surface, surface);
    extern const struct xdg_surface_interface xdg_surface_impl;
    wl_resource_set_implementation(surface->xdg_surface, &xdg_surface_impl, surface,
                                   xdg_surface_resource_destroy);
}
static void xdg_wm_pong(struct wl_client *client, struct wl_resource *resource, uint32_t serial) {
    (void)client; (void)resource; (void)serial;
}
static const struct xdg_wm_base_interface xdg_wm_impl = {
    .destroy = xdg_wm_destroy,
    .create_positioner = xdg_wm_positioner,
    .get_xdg_surface = xdg_wm_get_surface,
    .pong = xdg_wm_pong,
};
static void bind_xdg_wm(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    struct wl_resource *resource = wl_resource_create(client, &xdg_wm_base_interface,
                                                       version, id);
    if (resource) wl_resource_set_implementation(resource, &xdg_wm_impl, data, NULL);
}

static void decoration_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}
static void decoration_resource_destroy(struct wl_resource *resource) {
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    if (surface && surface->decoration == resource) surface->decoration = NULL;
}
static void decoration_set_mode(struct wl_client *client, struct wl_resource *resource,
                                uint32_t mode) {
    (void)client;
    if (mode != ZXDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE
        && mode != ZXDG_TOPLEVEL_DECORATION_V1_MODE_CLIENT_SIDE) {
        wl_resource_post_error(resource, ZXDG_TOPLEVEL_DECORATION_V1_ERROR_INVALID_MODE,
                               "invalid decoration mode");
        return;
    }
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    zxdg_toplevel_decoration_v1_send_configure(
        resource, ZXDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE);
    if (surface && surface->xdg_surface)
        xdg_surface_send_configure(surface->xdg_surface,
                                   wl_display_next_serial(surface->server->display));
}
static void decoration_unset_mode(struct wl_client *client, struct wl_resource *resource) {
    decoration_set_mode(client, resource, ZXDG_TOPLEVEL_DECORATION_V1_MODE_CLIENT_SIDE);
}
static const struct zxdg_toplevel_decoration_v1_interface decoration_impl = {
    .destroy = decoration_destroy,
    .set_mode = decoration_set_mode,
    .unset_mode = decoration_unset_mode,
};
static void decoration_manager_destroy(struct wl_client *client,
                                       struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}
static void decoration_manager_get_toplevel(struct wl_client *client,
                                             struct wl_resource *manager,
                                             uint32_t id,
                                             struct wl_resource *toplevel) {
    struct nx_surface *surface = wl_resource_get_user_data(toplevel);
    if (!surface || surface->toplevel != toplevel || surface->decoration) {
        wl_resource_post_error(manager, ZXDG_TOPLEVEL_DECORATION_V1_ERROR_ALREADY_CONSTRUCTED,
                               "toplevel decoration already exists or has no role");
        return;
    }
    surface->decoration = wl_resource_create(
        client, &zxdg_toplevel_decoration_v1_interface,
        wl_resource_get_version(manager), id);
    if (!surface->decoration) return;
    wl_resource_set_implementation(surface->decoration, &decoration_impl, surface,
                                   decoration_resource_destroy);
    zxdg_toplevel_decoration_v1_send_configure(
        surface->decoration, ZXDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE);
}
static const struct zxdg_decoration_manager_v1_interface decoration_manager_impl = {
    .destroy = decoration_manager_destroy,
    .get_toplevel_decoration = decoration_manager_get_toplevel,
};
static void bind_decoration_manager(struct wl_client *client, void *data,
                                    uint32_t version, uint32_t id) {
    struct wl_resource *resource = wl_resource_create(
        client, &zxdg_decoration_manager_v1_interface, version > 2 ? 2 : version, id);
    if (resource)
        wl_resource_set_implementation(resource, &decoration_manager_impl, data, NULL);
}

static void xdg_surface_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client; wl_resource_destroy(resource);
}
static void xdg_surface_resource_destroy(struct wl_resource *resource) {
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    if (surface && surface->xdg_surface == resource) surface->xdg_surface = NULL;
}
static void xdg_surface_get_toplevel(struct wl_client *client, struct wl_resource *resource,
                                     uint32_t id) {
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    if (surface->role != NX_ROLE_NONE || surface->toplevel || surface->popup) {
        wl_resource_post_error(resource, XDG_SURFACE_ERROR_ALREADY_CONSTRUCTED,
                               "xdg surface already has a role");
        return;
    }
    surface->toplevel = wl_resource_create(client, &xdg_toplevel_interface,
                                            wl_resource_get_version(resource), id);
    if (!surface->toplevel) return;
    surface->role = NX_ROLE_TOPLEVEL;
    extern const struct xdg_toplevel_interface xdg_toplevel_impl;
    wl_resource_set_implementation(surface->toplevel, &xdg_toplevel_impl, surface,
                                   xdg_toplevel_resource_destroy);
    struct wl_array states;
    wl_array_init(&states);
    xdg_toplevel_send_configure(surface->toplevel, 0, 0, &states);
    wl_array_release(&states);
    uint32_t serial = wl_display_next_serial(surface->server->display);
    xdg_surface_send_configure(resource, serial);
}
static void xdg_surface_get_popup(struct wl_client *client, struct wl_resource *resource,
                                  uint32_t id, struct wl_resource *parent,
                                  struct wl_resource *positioner) {
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    struct nx_surface *parent_surface = parent ? wl_resource_get_user_data(parent) : NULL;
    struct nx_positioner *position = positioner_data(positioner);
    if (!surface || surface->role != NX_ROLE_NONE || surface->toplevel || surface->popup || !parent_surface
        || (!parent_surface->toplevel && !parent_surface->popup)
        || !position || position->width <= 0 || position->height <= 0
        || position->rect_width < 0 || position->rect_height < 0) {
        wl_resource_post_error(resource, XDG_WM_BASE_ERROR_INVALID_POPUP_PARENT,
                               "popup needs a valid parent and positioner");
        return;
    }
    surface->role = NX_ROLE_POPUP;
    surface->popup_parent = parent_surface;
    surface->popup = wl_resource_create(client, &xdg_popup_interface,
                                         wl_resource_get_version(resource), id);
    if (!surface->popup) return;
    wl_resource_set_implementation(surface->popup, &xdg_popup_impl, surface,
                                   xdg_popup_resource_destroy);
    popup_configure(surface, position);
}
static void xdg_surface_set_geometry(struct wl_client *client, struct wl_resource *resource,
                                     int32_t x, int32_t y, int32_t width, int32_t height) {
    (void)client; (void)resource; (void)x; (void)y; (void)width; (void)height;
}
static void xdg_surface_ack_configure(struct wl_client *client, struct wl_resource *resource,
                                      uint32_t serial) {
    (void)client; (void)serial;
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    surface->configured = 1;
}
const struct xdg_surface_interface xdg_surface_impl = {
    .destroy = xdg_surface_destroy,
    .get_toplevel = xdg_surface_get_toplevel,
    .get_popup = xdg_surface_get_popup,
    .set_window_geometry = xdg_surface_set_geometry,
    .ack_configure = xdg_surface_ack_configure,
};

static void xdg_toplevel_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client; wl_resource_destroy(resource);
}
static void xdg_toplevel_resource_destroy(struct wl_resource *resource) {
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    if (surface && surface->toplevel == resource) surface->toplevel = NULL;
}
static void xdg_toplevel_set_parent(struct wl_client *client, struct wl_resource *resource,
                                    struct wl_resource *parent) {
    (void)client; (void)resource; (void)parent;
}
static void xdg_toplevel_set_title(struct wl_client *client, struct wl_resource *resource,
                                   const char *title) {
    (void)client;
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    snprintf(surface->title, sizeof(surface->title), "%s", title ? title : "Wayland app");
    queue_event(surface, NX_TITLE, NULL, 0, 0, 0, 0);
}
static void xdg_toplevel_set_app_id(struct wl_client *client, struct wl_resource *resource,
                                    const char *app_id) {
    (void)client;
    struct nx_surface *surface = wl_resource_get_user_data(resource);
    snprintf(surface->app_id, sizeof(surface->app_id), "%s", app_id ? app_id : "wayland-app");
    queue_event(surface, NX_TITLE, NULL, 0, 0, 0, 0);
}
static void xdg_toplevel_noop(struct wl_client *client, struct wl_resource *resource) {
    (void)client; (void)resource;
}
static void xdg_toplevel_move(struct wl_client *client, struct wl_resource *resource,
                              struct wl_resource *seat, uint32_t serial) {
    (void)client; (void)resource; (void)seat; (void)serial;
}
static void xdg_toplevel_menu(struct wl_client *client, struct wl_resource *resource,
                              struct wl_resource *seat, uint32_t serial, int32_t x, int32_t y) {
    (void)client; (void)resource; (void)seat; (void)serial; (void)x; (void)y;
}
static void xdg_toplevel_resize(struct wl_client *client, struct wl_resource *resource,
                                struct wl_resource *seat, uint32_t serial, uint32_t edges) {
    (void)client; (void)resource; (void)seat; (void)serial; (void)edges;
}
static void xdg_toplevel_fullscreen(struct wl_client *client, struct wl_resource *resource,
                                   struct wl_resource *output) {
    (void)client; (void)resource; (void)output;
}
static void xdg_toplevel_set_min_size(struct wl_client *client, struct wl_resource *resource,
                                     int32_t width, int32_t height) {
    (void)client; (void)resource; (void)width; (void)height;
}
static void xdg_toplevel_set_max_size(struct wl_client *client, struct wl_resource *resource,
                                     int32_t width, int32_t height) {
    (void)client; (void)resource; (void)width; (void)height;
}
const struct xdg_toplevel_interface xdg_toplevel_impl = {
    .destroy = xdg_toplevel_destroy,
    .set_parent = xdg_toplevel_set_parent,
    .set_title = xdg_toplevel_set_title,
    .set_app_id = xdg_toplevel_set_app_id,
    .show_window_menu = xdg_toplevel_menu,
    .move = xdg_toplevel_move,
    .resize = xdg_toplevel_resize,
    .set_max_size = xdg_toplevel_set_max_size,
    .set_min_size = xdg_toplevel_set_min_size,
    .set_maximized = xdg_toplevel_noop,
    .unset_maximized = xdg_toplevel_noop,
    .set_fullscreen = xdg_toplevel_fullscreen,
    .unset_fullscreen = xdg_toplevel_noop,
    .set_minimized = xdg_toplevel_noop,
};

struct nx_server *nexa_wl_create(const char *runtime_dir, const char *socket_name,
                                 int output_width, int output_height) {
    if (!runtime_dir || !socket_name || setenv("XDG_RUNTIME_DIR", runtime_dir, 1)) return NULL;
    struct nx_server *server = calloc(1, sizeof(*server));
    if (!server) return NULL;
    pthread_mutex_init(&server->event_lock, NULL);
    pthread_mutex_init(&server->api_lock, NULL);
    server->wake_read = server->wake_write = -1;
    server->keymap_fd = -1;
    wl_list_init(&server->surfaces);
    wl_list_init(&server->pointers);
    wl_list_init(&server->keyboards);
    wl_list_init(&server->outputs);
    wl_list_init(&server->text_inputs);
    server->output_width = output_width > 0 ? output_width : 1280;
    server->output_height = output_height > 0 ? output_height : 800;
    server->display = wl_display_create();
    int wake_fds[2];
    if (pipe(wake_fds) == 0) {
        server->wake_read = wake_fds[0];
        server->wake_write = wake_fds[1];
        fcntl(server->wake_read, F_SETFL, fcntl(server->wake_read, F_GETFL) | O_NONBLOCK);
        fcntl(server->wake_write, F_SETFL, fcntl(server->wake_write, F_GETFL) | O_NONBLOCK);
        fcntl(server->wake_read, F_SETFD, FD_CLOEXEC);
        fcntl(server->wake_write, F_SETFD, FD_CLOEXEC);
    }
    if (!server->display || server->wake_read < 0
        || wl_display_add_socket(server->display, socket_name) < 0
        || wl_display_init_shm(server->display) < 0) {
        if (server->display) wl_display_destroy(server->display);
        if (server->wake_read >= 0) close(server->wake_read);
        if (server->wake_write >= 0) close(server->wake_write);
        pthread_mutex_destroy(&server->event_lock);
        pthread_mutex_destroy(&server->api_lock);
        free(server);
        return NULL;
    }
    static const char keymap[] =
        "xkb_keymap {\n"
        " xkb_keycodes { include \"evdev+aliases(qwerty)\" };\n"
        " xkb_types { include \"complete\" };\n"
        " xkb_compat { include \"complete\" };\n"
        " xkb_symbols { include \"pc+us+inet(evdev)\" };\n"
        " xkb_geometry { include \"pc(pc104)\" };\n"
        "};\n";
    FILE *keymap_file = tmpfile();
    if (keymap_file) {
        if (fwrite(keymap, 1, sizeof(keymap), keymap_file) == sizeof(keymap)
            && fflush(keymap_file) == 0)
            server->keymap_fd = dup(fileno(keymap_file));
        fclose(keymap_file);
    }
    server->compositor = wl_global_create(server->display, &wl_compositor_interface, 5,
                                           server, bind_compositor);
    server->shell = wl_global_create(server->display, &xdg_wm_base_interface, 6,
                                      server, bind_xdg_wm);
    server->decoration_manager = wl_global_create(server->display,
        &zxdg_decoration_manager_v1_interface, 2, server, bind_decoration_manager);
    server->output = wl_global_create(server->display, &wl_output_interface, 4,
                                       server, bind_output);
    server->seat = wl_global_create(server->display, &wl_seat_interface, 7,
                                     server, bind_seat);
    server->text_input_manager = wl_global_create(server->display,
        &zwp_text_input_manager_v3_interface, 1, server, bind_text_input_manager);
    server->wake_source = wl_event_loop_add_fd(wl_display_get_event_loop(server->display),
        server->wake_read, WL_EVENT_READABLE, wake_dispatch, server);
    if (!server->compositor || !server->shell || !server->decoration_manager
        || !server->output || !server->seat || !server->text_input_manager
        || !server->wake_source) {
        wl_display_destroy(server->display);
        close(server->wake_read);
        close(server->wake_write);
        pthread_mutex_destroy(&server->event_lock);
        pthread_mutex_destroy(&server->api_lock);
        free(server);
        return NULL;
    }
    return server;
}

int nexa_wl_dispatch(struct nx_server *server, int timeout_ms) {
    if (!server) return -1;
    pthread_mutex_lock(&server->api_lock);
    struct wl_event_loop *loop = wl_display_get_event_loop(server->display);
    int result = wl_event_loop_dispatch(loop, timeout_ms);
    wl_display_flush_clients(server->display);
    pthread_mutex_unlock(&server->api_lock);
    return result;
}

int nexa_wl_next_event(struct nx_server *server, struct nx_event **out) {
    if (!server || !out) return -1;
    pthread_mutex_lock(&server->event_lock);
    struct nx_event *event = server->events_head;
    if (event) {
        server->events_head = event->next;
        if (!server->events_head) server->events_tail = NULL;
    }
    pthread_mutex_unlock(&server->event_lock);
    *out = event;
    return event ? 1 : 0;
}

void nexa_wl_free_event(struct nx_event *event) {
    if (!event) return;
    free(event->pixels);
    free(event);
}

void nexa_wl_close_toplevel(struct nx_server *server, uint64_t id) {
    if (!server) return;
    wake_server(server);
    pthread_mutex_lock(&server->api_lock);
    struct nx_surface *surface;
    wl_list_for_each(surface, &server->surfaces, link) {
        if (surface->id == id && surface->toplevel) {
            xdg_toplevel_send_close(surface->toplevel);
            wl_display_flush_clients(server->display);
            pthread_mutex_unlock(&server->api_lock);
            return;
        }
    }
    pthread_mutex_unlock(&server->api_lock);
}

static void set_keyboard_focus(struct nx_server *server, struct nx_surface *surface) {
    struct wl_client *client = surface ? wl_resource_get_client(surface->resource) : NULL;
    uint32_t serial = wl_display_next_serial(server->display);
    struct nx_keyboard *keyboard;
    wl_list_for_each(keyboard, &server->keyboards, link) {
        struct nx_surface *focus = surface && keyboard->client == client ? surface : NULL;
        if (keyboard->focus == focus) continue;
        if (keyboard->focus)
            wl_keyboard_send_leave(keyboard->resource, serial, keyboard->focus->resource);
        keyboard->focus = focus;
        if (!focus) keyboard->depressed = 0;
        if (focus) {
            struct wl_array keys;
            wl_array_init(&keys);
            wl_keyboard_send_enter(keyboard->resource, serial, focus->resource, &keys);
            wl_array_release(&keys);
            if (wl_resource_get_version(keyboard->resource) >= 4)
                wl_keyboard_send_modifiers(keyboard->resource, serial, keyboard->depressed,
                                           0, keyboard->locked, 0);
        }
    }
    struct nx_text_input *input;
    wl_list_for_each(input, &server->text_inputs, link) {
        struct nx_surface *focus = surface && input->client == client ? surface : NULL;
        if (input->surface == focus) continue;
        if (input->surface) {
            if (input->enabled)
                queue_event(input->surface, NX_TEXT_INPUT, NULL, 0, 0, 0, 0);
            zwp_text_input_v3_send_leave(input->resource, input->surface->resource);
        }
        input->surface = focus;
        if (focus) {
            zwp_text_input_v3_send_enter(input->resource, focus->resource);
            if (input->enabled)
                queue_event(focus, NX_TEXT_INPUT, NULL, 1, 0, 0, 0);
        }
    }
}

void nexa_wl_text_input_commit(struct nx_server *server, uint64_t id, const char *text) {
    if (!server || !text) return;
    wake_server(server);
    pthread_mutex_lock(&server->api_lock);
    struct nx_surface *surface = NULL, *cursor;
    wl_list_for_each(cursor, &server->surfaces, link)
        if (cursor->id == id) { surface = cursor; break; }
    if (surface) {
        struct nx_text_input *input;
        uint32_t serial = ++server->text_input_serial;
        wl_list_for_each(input, &server->text_inputs, link) {
            if (input->surface != surface || !input->enabled) continue;
            zwp_text_input_v3_send_commit_string(input->resource, text);
            zwp_text_input_v3_send_done(input->resource, serial);
        }
        wl_display_flush_clients(server->display);
    }
    pthread_mutex_unlock(&server->api_lock);
}

void nexa_wl_text_input_delete(struct nx_server *server, uint64_t id,
                               uint32_t before, uint32_t after) {
    if (!server) return;
    wake_server(server);
    pthread_mutex_lock(&server->api_lock);
    struct nx_surface *surface = NULL, *cursor;
    wl_list_for_each(cursor, &server->surfaces, link)
        if (cursor->id == id) { surface = cursor; break; }
    if (surface) {
        struct nx_text_input *input;
        uint32_t serial = ++server->text_input_serial;
        wl_list_for_each(input, &server->text_inputs, link) {
            if (input->surface != surface || !input->enabled) continue;
            zwp_text_input_v3_send_delete_surrounding_text(input->resource, before, after);
            zwp_text_input_v3_send_done(input->resource, serial);
        }
        wl_display_flush_clients(server->display);
    }
    pthread_mutex_unlock(&server->api_lock);
}

void nexa_wl_focus(struct nx_server *server, uint64_t id) {
    if (!server) return;
    wake_server(server);
    pthread_mutex_lock(&server->api_lock);
    struct nx_surface *surface = NULL, *cursor;
    if (id != 0) {
        wl_list_for_each(cursor, &server->surfaces, link) {
            if (cursor->id == id) { surface = cursor; break; }
        }
        if (!surface) {
            pthread_mutex_unlock(&server->api_lock);
            return;
        }
    }
    set_keyboard_focus(server, surface);
    wl_display_flush_clients(server->display);
    pthread_mutex_unlock(&server->api_lock);
}

void nexa_wl_configure_toplevel(struct nx_server *server, uint64_t id,
                                int width, int height) {
    if (!server || width < 0 || height < 0) return;
    wake_server(server);
    pthread_mutex_lock(&server->api_lock);
    struct nx_surface *surface = NULL, *cursor;
    wl_list_for_each(cursor, &server->surfaces, link)
        if (cursor->id == id) { surface = cursor; break; }
    if (surface && surface->toplevel && surface->xdg_surface) {
        struct wl_array states;
        wl_array_init(&states);
        xdg_toplevel_send_configure(surface->toplevel, width, height, &states);
        wl_array_release(&states);
        xdg_surface_send_configure(surface->xdg_surface,
                                   wl_display_next_serial(server->display));
        wl_display_flush_clients(server->display);
    }
    pthread_mutex_unlock(&server->api_lock);
}

void nexa_wl_update_output(struct nx_server *server, int width, int height) {
    if (!server || width <= 0 || height <= 0) return;
    wake_server(server);
    pthread_mutex_lock(&server->api_lock);
    int old_width = server->output_width, old_height = server->output_height;
    server->output_width = width;
    server->output_height = height;
    struct nx_output *output;
    wl_list_for_each(output, &server->outputs, link) {
        wl_output_send_mode(output->resource, 0, old_width, old_height, 60000);
        wl_output_send_mode(output->resource, WL_OUTPUT_MODE_CURRENT, width, height, 60000);
        if (wl_resource_get_version(output->resource) >= 2)
            wl_output_send_done(output->resource);
    }
    wl_display_flush_clients(server->display);
    pthread_mutex_unlock(&server->api_lock);
}

void nexa_wl_pointer(struct nx_server *server, uint64_t id, int action,
                     int x, int y, uint32_t time, uint32_t button, int pressed) {
    if (!server) return;
    wake_server(server);
    pthread_mutex_lock(&server->api_lock);
    struct nx_surface *surface = NULL, *cursor;
    wl_list_for_each(cursor, &server->surfaces, link)
        if (cursor->id == id) { surface = cursor; break; }
    if (!surface) { pthread_mutex_unlock(&server->api_lock); return; }
    uint32_t serial = wl_display_next_serial(server->display);
    struct nx_pointer *pointer;
    wl_list_for_each(pointer, &server->pointers, link) {
        if (pointer->client != wl_resource_get_client(surface->resource)) continue;
        if (action == 0 && pointer->focus != surface) {
            if (pointer->focus)
                wl_pointer_send_leave(pointer->resource, serial, pointer->focus->resource);
            pointer->focus = surface;
            wl_pointer_send_enter(pointer->resource, serial, surface->resource,
                                  wl_fixed_from_int(x), wl_fixed_from_int(y));
        } else if (action == 3 && pointer->focus == surface) {
            wl_pointer_send_leave(pointer->resource, serial, surface->resource);
            pointer->focus = NULL;
        }
        if (action == 0 || action == 1 || action == 2) {
            if (pointer->focus == surface)
                wl_pointer_send_motion(pointer->resource, time,
                                       wl_fixed_from_int(x), wl_fixed_from_int(y));
        }
        if (action == 2 && pointer->focus == surface) {
            wl_pointer_send_button(pointer->resource, serial, time, button,
                                   pressed ? WL_POINTER_BUTTON_STATE_PRESSED : WL_POINTER_BUTTON_STATE_RELEASED);
            if (pressed) set_keyboard_focus(server, surface);
        }
        if (action == 4 && pointer->focus == surface) {
            if (x != 0) wl_pointer_send_axis(pointer->resource, time,
                                              WL_POINTER_AXIS_HORIZONTAL_SCROLL,
                                              wl_fixed_from_int(x));
            if (y != 0) wl_pointer_send_axis(pointer->resource, time,
                                              WL_POINTER_AXIS_VERTICAL_SCROLL,
                                              wl_fixed_from_int(y));
        }
        if (wl_resource_get_version(pointer->resource) >= 5) wl_pointer_send_frame(pointer->resource);
    }
    wl_display_flush_clients(server->display);
    pthread_mutex_unlock(&server->api_lock);
}

void nexa_wl_key(struct nx_server *server, uint64_t id, uint32_t time,
                 uint32_t key, int pressed) {
    if (!server) return;
    wake_server(server);
    pthread_mutex_lock(&server->api_lock);
    struct nx_surface *surface = NULL, *cursor;
    wl_list_for_each(cursor, &server->surfaces, link)
        if (cursor->id == id) { surface = cursor; break; }
    if (!surface) { pthread_mutex_unlock(&server->api_lock); return; }
    uint32_t serial = wl_display_next_serial(server->display);
    struct nx_keyboard *keyboard;
    wl_list_for_each(keyboard, &server->keyboards, link) {
        if (keyboard->client != wl_resource_get_client(surface->resource)) continue;
        if (keyboard->focus != surface) {
            if (keyboard->focus)
                wl_keyboard_send_leave(keyboard->resource, serial, keyboard->focus->resource);
            keyboard->focus = surface;
            struct wl_array keys;
            wl_array_init(&keys);
            wl_keyboard_send_enter(keyboard->resource, serial, surface->resource, &keys);
            wl_array_release(&keys);
        }
        wl_keyboard_send_key(keyboard->resource, serial, time, key,
                             pressed ? WL_KEYBOARD_KEY_STATE_PRESSED : WL_KEYBOARD_KEY_STATE_RELEASED);
        if (key == 42 || key == 54) {
            if (pressed) keyboard->depressed |= 1u;
            else keyboard->depressed &= ~1u;
        } else if (key == 29 || key == 97) {
            if (pressed) keyboard->depressed |= 1u << 2;
            else keyboard->depressed &= ~(1u << 2);
        } else if (key == 56) {
            if (pressed) keyboard->depressed |= 1u << 3;
            else keyboard->depressed &= ~(1u << 3);
        } else if (key == 100) {
            if (pressed) keyboard->depressed |= 1u << 7;
            else keyboard->depressed &= ~(1u << 7);
        } else if (key == 125 || key == 126) {
            if (pressed) keyboard->depressed |= 1u << 6;
            else keyboard->depressed &= ~(1u << 6);
        } else if (key == 58) {
            if (pressed && !keyboard->caps_down) keyboard->locked ^= 1u << 1;
            keyboard->caps_down = pressed;
        } else if (key == 69) {
            if (pressed && !keyboard->num_down) keyboard->locked ^= 1u << 4;
            keyboard->num_down = pressed;
        }
        if (wl_resource_get_version(keyboard->resource) >= 4)
            wl_keyboard_send_modifiers(keyboard->resource, serial, keyboard->depressed,
                                       0, keyboard->locked, 0);
    }
    wl_display_flush_clients(server->display);
    pthread_mutex_unlock(&server->api_lock);
}

void nexa_wl_destroy(struct nx_server *server) {
    if (!server) return;
    wl_display_destroy_clients(server->display);
    wl_display_destroy(server->display);
    if (server->keymap_fd >= 0) close(server->keymap_fd);
    close(server->wake_read);
    close(server->wake_write);
    struct nx_event *event = server->events_head;
    while (event) { struct nx_event *next = event->next; nexa_wl_free_event(event); event = next; }
    pthread_mutex_destroy(&server->event_lock);
    pthread_mutex_destroy(&server->api_lock);
    free(server);
}

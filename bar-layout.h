#ifndef WHITECUBE_BAR_LAYOUT_H
#define WHITECUBE_BAR_LAYOUT_H

// Shared by the compositor's tab reservation and the status client's widgets.
// Dimensions use output logical pixels after the desktop UI scale is applied.
struct wc_bar_layout { int status_width, chat_x, chat_width, tabs_end; };

static inline struct wc_bar_layout wc_bar_layout(int width, double scale) {
	int apps = (int)(104 * scale), right = (int)(160 * scale);
	int gap = (int)(4 * scale), available = width - apps - right - 3 * gap;
	int status = (int)(300 * scale), chat = (int)((width / scale >= 1100 ? 140 : 80) * scale);
	int minimum_chat = (int)(52 * scale);
	if (status > available - minimum_chat) status = available - minimum_chat;
	if (status < 1) status = 1;
	if (chat > available - status) chat = available - status;
	if (chat < 1) chat = 1;
	int status_left = width - right - status;
	int x = (width - chat) / 2;
	if (x + chat > status_left - gap) x = status_left - gap - chat;
	if (x < apps + gap) x = apps + gap;
	return (struct wc_bar_layout){ status, x, chat, x - gap };
}

#endif

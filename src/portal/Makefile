# SPDX-License-Identifier: MIT
# whitecube-portal - zero-dependency org.freedesktop.portal.Desktop
#
#   make            release build (hardened PIE)
#   make test       unit tests + full D-Bus integration suite
#   make sanitize   ASan/UBSan build (build/whitecube-portal-asan)
#   make install    install binary + wc-pick

CC       ?= cc
PREFIX   ?= /usr/local
BINDIR   ?= $(PREFIX)/bin
DESTDIR  ?=

WARN      = -Wall -Wextra -Werror
SEC       = -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3 -fstack-protector-strong -fPIE
SEC_LD    = -pie -Wl,-z,relro,-z,now -Wl,-z,noexecstack
CPPFLAGS += -Iinclude -D_GNU_SOURCE -DWC_DEFAULT_PICKER='"$(BINDIR)/wc-pick"'
CFLAGS   ?= -O2
CFLAGS   += -std=c11 $(WARN) $(SEC)
LDFLAGS  += $(SEC_LD)

BUILD    = build
BIN      = $(BUILD)/whitecube-portal
SRC      = $(wildcard src/*.c)
OBJ      = $(patsubst src/%.c,$(BUILD)/%.o,$(SRC))
LIBOBJ   = $(filter-out $(BUILD)/main.o,$(OBJ))
DEPS     = $(OBJ:.o=.d)

.PHONY: all clean install test unit integration sanitize check help
all: $(BIN)

$(BIN): $(OBJ)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(OBJ)

$(BUILD)/%.o: src/%.c | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -MMD -MP -c $< -o $@

$(BUILD):
	mkdir -p $(BUILD)

# ---- tests -------------------------------------------------------------
$(BUILD)/test_wire: tests/test_wire.c $(BUILD)/dbus_wire.o $(BUILD)/util.o | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $^

$(BUILD)/portal-probe: tests/portal-probe.c $(LIBOBJ) | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $^

unit: $(BUILD)/test_wire
	$(BUILD)/test_wire

integration: $(BIN) $(BUILD)/portal-probe
	WC_PORTAL_BIN=$(abspath $(BIN)) WC_PROBE_BIN=$(abspath $(BUILD)/portal-probe) \
	  sh tests/run-tests.sh

test: unit integration

check: test

# ---- sanitizer build ---------------------------------------------------
sanitize:
	mkdir -p $(BUILD)
	$(CC) $(CPPFLAGS) -std=c11 $(WARN) -O1 -g -fsanitize=address,undefined \
	  -fno-omit-frame-pointer -o $(BUILD)/whitecube-portal-asan $(SRC)
	$(CC) $(CPPFLAGS) -std=c11 $(WARN) -O1 -g -fsanitize=address,undefined \
	  -fno-omit-frame-pointer -o $(BUILD)/portal-probe-asan \
	  tests/portal-probe.c $(filter-out src/main.c,$(SRC))

install: $(BIN)
	install -d $(DESTDIR)$(BINDIR)
	install -m 0755 $(BIN) $(DESTDIR)$(BINDIR)/whitecube-portal
	install -m 0755 scripts/wc-pick $(DESTDIR)$(BINDIR)/wc-pick

clean:
	rm -rf $(BUILD)

help:
	@echo "targets: all, test, unit, integration, sanitize, install, clean"

-include $(DEPS)
CC       ?= cc
PREFIX   ?= /usr/local
BINDIR   ?= $(PREFIX)/bin
DESTDIR  ?=
CPPFLAGS += -Iinclude -D_GNU_SOURCE -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3
CFLAGS   ?= -std=c11 -O2 -Wall -Wextra -Werror -fstack-protector-strong -fPIE
LDFLAGS  += -pie -Wl,-z,relro,-z,now -Wl,-z,noexecstack

BIN = whitecube-bus
SRC = src/main.c src/wire.c src/bus.c src/seccomp.c
OBJ = $(SRC:.c=.o)

all: $(BIN)

$(BIN): $(OBJ)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(OBJ)

src/%.o: src/%.c include/whitecube_bus.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

sanitize: $(SRC) include/whitecube_bus.h
	$(CC) -Iinclude -D_GNU_SOURCE -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -o $(BIN)-asan $(SRC)

clean:
	rm -f $(OBJ) $(BIN) $(BIN)-asan

install: $(BIN)
	install -Dm755 $(BIN) $(DESTDIR)$(BINDIR)/$(BIN)

.PHONY: all clean install sanitize

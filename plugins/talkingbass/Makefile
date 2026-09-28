BUNDLE = talkingbass.lv2
INSTALL_DIR = $(HOME)/.lv2/$(BUNDLE)
CFLAGS ?= -O2 -fPIC -Wall -Wextra -std=c99 -fno-strict-aliasing

all: talkingbass.so

talkingbass.so: talkingbass.c
	$(CC) $(CFLAGS) -shared -o $@ talkingbass.c -lm

install: talkingbass.so manifest.ttl talkingbass.ttl
	mkdir -p "$(INSTALL_DIR)"
	cp -f talkingbass.so manifest.ttl talkingbass.ttl "$(INSTALL_DIR)/"

clean:
	rm -f talkingbass.so

.PHONY: all install clean

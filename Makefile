.PHONY: all

all: magnifica-humanitas.epub

magnifica-humanitas.html:
	curl -L -o $@ https://www.vatican.va/content/leo-xiv/en/encyclicals/documents/20260515-magnifica-humanitas.html

magnifica-humanitas.epub: magnifica-humanitas.html cover.png
	python3 magnifica-humanitas.py $^ $@

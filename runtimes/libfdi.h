// libfdi.h -- funzioni di input usate negli esempi e negli esercizi della
// dispensa. Per usarle basta includere questo file:
//
//   #include "libfdi.h"
//
// Simile a input() di Python ma senza il prompt e specializzato per tipi
// di dato diversi.,
//
//   int n = input_int();
//
// Le funzioni terminano il programma con exit(EXIT_FAILURE) se l'input non
// e' valido (fine dello standard input, o testo che non rappresenta un
// numero): non c'e' un modo per recuperare l'errore, quindi vanno usate solo
// su input che ci si aspetta corretto (per esempio nei primi esercizi, prima
// di aver introdotto una gestione degli errori piu' fine).

#ifndef LIBFDI_H
#define LIBFDI_H

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdbool.h>
#include <ctype.h>
#include <errno.h>
#include <limits.h>

// Stampa il messaggio e svuota il buffer di stdout, altrimenti il
// messaggio, che non termina con '\n', potrebbe comparire solo dopo che
// l'utente ha gia' scritto la risposta.
static inline void print_prompt(char *prompt) {
  printf("%s", prompt);
  fflush(stdout);
}

// Vero se dalla posizione end fino alla fine della stringa ci sono solo
// spazi: serve a rifiutare righe come "12abc", che sscanf o strtol da soli
// leggerebbero come 12. Come int() di Python, gli spazi ai lati del numero
// sono invece ammessi.
static inline bool only_spaces(char *end) {
  while (*end != '\0') {
    if (!isspace((unsigned char) *end)) {
      return false;
    }
    end++;
  }
  return true;
}


// Legge una riga da standard input, senza limiti sulla sua lunghezza, e la
// restituisce come stringa allocata sullo heap, senza il carattere '\n'
// finale. Chi chiama la funzione e' responsabile di liberare la stringa
// con free() quando non serve piu'.
static inline char * input_string(void) {
  // Non sapendo in anticipo quanto e' lunga la riga, si parte da un buffer
  // piccolo e lo si raddoppia ogni volta che si scopre che non basta.
  size_t bufsize = 2;
  size_t maxread = bufsize;
  size_t offset = 0;
  char *string = malloc(bufsize);
  if (string == NULL) {
      fprintf(stderr, "out of memory\n");
      exit(EXIT_FAILURE);
  }
  while (true) {
    // Si continua a leggere a partire da dove ci si era fermati (offset),
    // cosi' fgets non sovrascrive quanto gia' letto nei giri precedenti.
    if (fgets(string + offset, maxread, stdin) == NULL) {
      if (offset > 0) {
        // Lo standard input e' finito senza un '\n' finale (per esempio
        // un file che non termina con una riga vuota): i caratteri letti
        // nei giri precedenti sono comunque una riga valida.
        return string;
      }
      fprintf(stderr, "could not read from standard input\n");
      exit(EXIT_FAILURE);
    }
    size_t len=strlen(string);
    if (string[len-1] == '\n') {
      // La riga e' stata letta per intero: rimuovi il carattere '\n'
      // finale e restituisci la stringa.
      string[strcspn(string, "\n")] = '\0';
      return string;
    }
    // Il buffer si e' riempito senza trovare '\n': la riga continua,
    // quindi raddoppia la dimensione della stringa e leggi il resto.
    char *new_string = realloc(string, 2*bufsize);
    if (new_string == NULL) {
      fprintf(stderr, "out of memory\n");
      exit(EXIT_FAILURE);
    }
    string = new_string;
    offset = len;
    bufsize = 2 * bufsize;
    maxread = bufsize - offset;
  }
}

// Legge una riga da standard input e la interpreta come un intero.
// Usa input_string() e libera la stringa temporanea prima di restituire
// il valore, quindi non lascia perdite di memoria. Rifiuta, come int() di
// Python, una riga vuota, una riga con altro testo dopo il numero e un
// numero che non sta in un int.
static inline int input_int(void) {
  char *buf = input_string();
  char *end;
  errno = 0;
  long x = strtol(buf, &end, 10);
  if (end == buf || !only_spaces(end)) {
    fprintf(stderr, "not an integer: '%s'\n", buf);
    free(buf);
    exit(EXIT_FAILURE);
  }
  if (errno == ERANGE || x < INT_MIN || x > INT_MAX) {
    fprintf(stderr, "integer out of range: '%s'\n", buf);
    free(buf);
    exit(EXIT_FAILURE);
  }
  free(buf);
  return x;
}

// Come input_int(), ma interpreta la riga letta come un numero in
// virgola mobile (per esempio "3.14").
static inline double input_double(void) {
  char *buf = input_string();
  char *end;
  errno = 0;
  double x = strtod(buf, &end);
  if (end == buf || !only_spaces(end)) {
    fprintf(stderr, "not a number: '%s'\n", buf);
    free(buf);
    exit(EXIT_FAILURE);
  }
  // strtod segnala ERANGE sia per i numeri troppo grandi sia per quelli
  // troppo vicini a zero: solo i primi sono un errore (i secondi valgono
  // 0, come in Python).
  if (errno == ERANGE && (x > 1 || x < -1)) {
    fprintf(stderr, "number out of range: '%s'\n", buf);
    free(buf);
    exit(EXIT_FAILURE);
  }
  free(buf);
  return x;
}


#endif // LIBFDI_H

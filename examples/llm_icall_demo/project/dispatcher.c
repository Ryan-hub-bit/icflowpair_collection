#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int (*operation_fn)(int);

#if defined(__GNUC__) || defined(__clang__)
#define NOINLINE __attribute__((noinline))
#else
#define NOINLINE
#endif

static NOINLINE int increment(int value) { return value + 1; }
static NOINLINE int decrement(int value) { return value - 1; }
static NOINLINE int double_value(int value) { return value * 2; }
static NOINLINE int same_type_not_address_taken(int value) { return value; }

static operation_fn select_operation(const char *name) {
    if (strcmp(name, "increment") == 0) {
        return increment;
    }
    if (strcmp(name, "decrement") == 0) {
        return decrement;
    }
    if (strcmp(name, "double") == 0) {
        return double_value;
    }
    return NULL;
}

static NOINLINE int dispatch(operation_fn operation, int value) {
    volatile operation_fn selected = operation;
    return selected(value);
}

int main(int argc, char **argv) {
    char *end = NULL;
    long expected;
    int actual;
    operation_fn operation;

    if (argc != 3) {
        fprintf(stderr, "usage: %s OPERATION EXPECTED_RESULT\n", argv[0]);
        return 2;
    }
    operation = select_operation(argv[1]);
    if (operation == NULL) {
        fprintf(stderr, "unknown operation: %s\n", argv[1]);
        return 2;
    }
    errno = 0;
    expected = strtol(argv[2], &end, 10);
    if (errno != 0 || end == argv[2] || *end != '\0') {
        fprintf(stderr, "invalid expected result: %s\n", argv[2]);
        return 2;
    }
    actual = dispatch(operation, 41);
    if (same_type_not_address_taken(actual) != actual) {
        fprintf(stderr, "same-type direct-call check failed\n");
        return 1;
    }
    if (actual != expected) {
        fprintf(stderr, "%s returned %d, expected %ld\n", argv[1], actual, expected);
        return 1;
    }
    printf("%s -> %d\n", argv[1], actual);
    return 0;
}

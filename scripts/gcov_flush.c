#define _GNU_SOURCE
#include <signal.h>

#ifdef __cplusplus
extern "C" {
#endif
void __gcov_flush(void);
#ifdef __cplusplus
}
#endif

static void handler(int sig) {
    (void)sig;
    __gcov_flush();
}

__attribute__((constructor))
static void init_flush(void) {
    struct sigaction sa;
    sa.sa_handler = handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_RESTART;
    sigaction(SIGUSR1, &sa, 0);
}

/* lpcbench-netfwd: the jail's only way out of its empty network namespace.
 *
 *   lpcbench-netfwd 127.0.0.1:PORT=/run/lpcbench/NAME.sock [...] -- CMD ARGS...
 *
 * Listens on each loopback PORT inside the jail and relays every connection,
 * byte for byte, to the host's unix socket at the paired path (bind-mounted
 * into the jail). The host decides what each socket reaches (the model proxy,
 * or an allowlisting CONNECT proxy). Then it execs CMD; the relay process dies
 * with it (PR_SET_PDEATHSIG, and the jail's PID namespace).
 * Part of lpcbench/jail.py; built into the jail rootfs.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define MAXMAP 8

static void die(const char *what) {
    fprintf(stderr, "lpcbench-netfwd: %s: %s\n", what, strerror(errno));
    _exit(111);
}

static void pump(int a, int b) {
    char buf[65536];
    struct pollfd p[2] = {{a, POLLIN, 0}, {b, POLLIN, 0}};
    int open_a = 1, open_b = 1;
    while (open_a || open_b) {
        p[0].fd = open_a ? a : -1;
        p[1].fd = open_b ? b : -1;
        if (poll(p, 2, -1) < 0) {
            if (errno == EINTR) continue;
            break;
        }
        for (int i = 0; i < 2; i++) {
            if (!(p[i].revents & (POLLIN | POLLHUP | POLLERR))) continue;
            int from = i ? b : a, to = i ? a : b;
            ssize_t n = read(from, buf, sizeof buf);
            if (n <= 0) {
                shutdown(to, SHUT_WR);
                if (i) open_b = 0; else open_a = 0;
                continue;
            }
            for (ssize_t off = 0; off < n;) {
                ssize_t w = write(to, buf + off, (size_t)(n - off));
                if (w <= 0) _exit(0);
                off += w;
            }
        }
    }
    _exit(0);
}

int main(int argc, char **argv) {
    int lfd[MAXMAP];
    const char *path[MAXMAP];
    int n = 0, i = 1;
    for (; i < argc && strcmp(argv[i], "--") != 0; i++) {
        if (n == MAXMAP) { fprintf(stderr, "lpcbench-netfwd: too many maps\n"); return 111; }
        char *spec = argv[i], *eq = strchr(spec, '='), *colon = strrchr(spec, ':');
        if (!eq || !colon || colon > eq) { fprintf(stderr, "lpcbench-netfwd: bad map %s\n", spec); return 111; }
        *eq = 0; *colon = 0;
        struct sockaddr_in sa = {0};
        sa.sin_family = AF_INET;
        sa.sin_port = htons((unsigned short)atoi(colon + 1));
        if (inet_pton(AF_INET, spec, &sa.sin_addr) != 1) { fprintf(stderr, "lpcbench-netfwd: bad address %s\n", spec); return 111; }
        int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
        int one = 1;
        if (fd < 0) die("socket");
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
        if (bind(fd, (struct sockaddr *)&sa, sizeof sa) < 0) die("bind");
        if (listen(fd, 64) < 0) die("listen");
        lfd[n] = fd;
        path[n] = eq + 1;
        n++;
    }
    if (i >= argc - 1) { fprintf(stderr, "usage: lpcbench-netfwd ADDR:PORT=SOCK... -- CMD...\n"); return 111; }
    pid_t parent = getpid();
    pid_t relay = fork();
    if (relay < 0) die("fork");
    if (relay == 0) {
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        if (getppid() != parent) _exit(0);
        signal(SIGCHLD, SIG_IGN);  /* connection children reap themselves */
        struct pollfd p[MAXMAP];
        for (int k = 0; k < n; k++) { p[k].fd = lfd[k]; p[k].events = POLLIN; }
        for (;;) {
            if (poll(p, (nfds_t)n, -1) < 0) { if (errno == EINTR) continue; _exit(111); }
            for (int k = 0; k < n; k++) {
                if (!(p[k].revents & POLLIN)) continue;
                int c = accept4(lfd[k], NULL, NULL, SOCK_CLOEXEC);
                if (c < 0) continue;
                pid_t h = fork();
                if (h == 0) {
                    prctl(PR_SET_PDEATHSIG, SIGKILL);
                    for (int j = 0; j < n; j++) close(lfd[j]);
                    int u = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
                    struct sockaddr_un ua = {0};
                    ua.sun_family = AF_UNIX;
                    strncpy(ua.sun_path, path[k], sizeof ua.sun_path - 1);
                    if (u < 0 || connect(u, (struct sockaddr *)&ua, sizeof ua) < 0) { close(c); _exit(0); }
                    pump(c, u);
                }
                close(c);
            }
        }
    }
    for (int k = 0; k < n; k++) close(lfd[k]);
    execvp(argv[i + 1], argv + i + 1);
    die(argv[i + 1]);
    return 111;
}

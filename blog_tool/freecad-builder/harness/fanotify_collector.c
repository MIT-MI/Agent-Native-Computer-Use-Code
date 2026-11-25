#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/fanotify.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <time.h>
#include <unistd.h>

/* Notification only. Resolve identity immediately on consuming the event,
 * before path/JSON work. PIDFD prevents PID reuse; it cannot resurrect an exited
 * process. Missing/dead PIDFD or unresolved credentials remain unknown. */
#ifndef FAN_REPORT_PIDFD
#define FAN_REPORT_PIDFD 0x00000080
#endif
#ifndef FAN_EVENT_INFO_TYPE_PIDFD
#define FAN_EVENT_INFO_TYPE_PIDFD 4
#endif
struct audit_pidfd_info { struct fanotify_event_info_header hdr; int32_t pidfd; };
static volatile sig_atomic_t stopping;
static void stop(int sig) { (void)sig; stopping = 1; }

static void json_string(const char *s) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
        if (*p == '"' || *p == '\\') printf("\\%c", *p);
        else if (*p < 32 || *p >= 127) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

static int pidfd_alive(int fd, pid_t pid) {
    struct pollfd p = {.fd=fd,.events=POLLIN};
    if (poll(&p,1,0) != 0) return 0;
    char *name, *line=NULL; size_t capacity=0;
    if (asprintf(&name,"/proc/self/fdinfo/%d",fd)<0) return 0;
    FILE *f=fopen(name,"r"); free(name); if (!f) return 0;
    int actual=-1;
    while (getline(&line,&capacity,f)>=0) if (sscanf(line,"Pid:\t%d",&actual)==1) break;
    free(line); fclose(f); return actual == pid;
}

static int effective_uid(pid_t pid, int pidfd, int pidfd_mode) {
    if (pid <= 0 || (pidfd_mode && (pidfd < 0 || !pidfd_alive(pidfd,pid)))) return -1;
    char *name, *line=NULL; size_t capacity=0;
    if (asprintf(&name,"/proc/%d",pid)<0) return -1;
    /* An open proc directory cannot silently turn into a recycled PID. */
    int proc=open(name,O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    free(name);
    if (proc<0) return -1;
    int status=openat(proc,"status",O_RDONLY|O_CLOEXEC);close(proc);
    if (status<0) return -1;
    FILE *file=fdopen(status,"r");
    if (!file) {close(status);return -1;}
    int real_uid, uid=-1;
    while (getline(&line,&capacity,file)>=0) {
        if (sscanf(line,"Uid:\t%d\t%d",&real_uid,&uid)==2) break;
    }
    free(line); fclose(file);
    if (pidfd_mode && !pidfd_alive(pidfd,pid)) return -1;
    return uid;
}

static int event_pidfd(struct fanotify_event_metadata *event, int *bad) {
    int fd=-1;
    unsigned char *cur=(unsigned char *)event+event->metadata_len;
    unsigned char *end=(unsigned char *)event+event->event_len;
    while (cur<end) {
        if ((size_t)(end-cur)<sizeof(struct fanotify_event_info_header)) {*bad=1;break;}
        struct fanotify_event_info_header *h=(void *)cur;
        if (h->len<sizeof(*h) || h->len>(size_t)(end-cur)) {*bad=1;break;}
        if (h->info_type==FAN_EVENT_INFO_TYPE_PIDFD) {
            if (h->len<sizeof(struct audit_pidfd_info) || fd>=0) {*bad=1;break;}
            fd=((struct audit_pidfd_info *)h)->pidfd;
        }
        cur+=h->len;
    }
    return fd;
}

int main(void) {
    if (geteuid() != 0) { fprintf(stderr, "collector requires root\n"); return EXIT_FAILURE; }
    if (prctl(PR_SET_DUMPABLE, 0) != 0) { perror("prctl"); return EXIT_FAILURE; }
    setvbuf(stdout, NULL, _IOLBF, 0);
    int pidfd_mode=1;
    int flags=FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK;
    int fan=fanotify_init(flags | FAN_REPORT_PIDFD,O_RDONLY | O_LARGEFILE | O_CLOEXEC);
    if (fan<0 && (errno==EINVAL || errno==EOPNOTSUPP)) {
        pidfd_mode=0;
        fan=fanotify_init(flags,O_RDONLY | O_LARGEFILE | O_CLOEXEC);
    }
    if (fan < 0) { perror("fanotify_init"); return EXIT_FAILURE; }
    {
        const char *path = "/";
        int result = fanotify_mark(fan, FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
                                  FAN_OPEN | FAN_OPEN_EXEC, AT_FDCWD, path);
        int saved_errno = result ? errno : 0;
        printf("{\"kind\":\"audit_mark\",\"path\":"); json_string(path);
        printf(",\"ok\":%s,\"errno\":%d}\n", result ? "false" : "true", saved_errno);
        if (result) { close(fan); return EXIT_FAILURE; }
    }
    struct sigaction action = {.sa_handler = stop};
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    printf("{\"kind\":\"audit_ready\",\"pid\":%d,\"uid\":%d,"
           "\"mark\":\"FAN_MARK_FILESYSTEM\",\"path\":\"/\","
           "\"class\":\"FAN_CLASS_NOTIF\",\"report_pidfd\":%s}\n", getpid(), geteuid(),pidfd_mode?"true":"false");
    unsigned long events = 0, unresolved = 0, overflow = 0, invalid = 0;
    struct pollfd poller = {.fd = fan, .events = POLLIN};
    while (!stopping) {
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now)) { ++invalid; break; }
        int ready = poll(&poller, 1, -1);
        if (ready < 0) { if (errno == EINTR) continue; perror("poll"); ++invalid; break; }
        if (!ready) continue;
        int queued;
        if (ioctl(fan, FIONREAD, &queued)<0) { perror("FIONREAD"); ++invalid; break; }
        void *buffer=malloc(queued);
        if (!buffer) { perror("malloc"); ++invalid; break; }
        ssize_t length = read(fan, buffer, queued);
        if (length < 0) {
            free(buffer);
            if (errno == EAGAIN || errno == EINTR) continue;
            perror("read"); ++invalid; break;
        }
        if (!length) { free(buffer); ++invalid; break; }
        struct fanotify_event_metadata *event = (void *)buffer;
        while (FAN_EVENT_OK(event, length)) {
            if (event->vers != FANOTIFY_METADATA_VERSION) { ++invalid; stopping = 1; break; }
            if (event->mask & FAN_Q_OVERFLOW) {
                ++overflow; puts("{\"kind\":\"audit_error\",\"reason\":\"overflow\"}");
            }
            int bad_info=0;
            int pidfd=event_pidfd(event,&bad_info);
            if (bad_info) ++invalid;
            int uid=effective_uid(event->pid,pidfd,pidfd_mode);
            if (event->fd >= 0) {
                char *fd_path, path[PATH_MAX + 1];
                if (asprintf(&fd_path,"/proc/self/fd/%d",event->fd)<0) { close(event->fd); ++invalid; break; }
                ssize_t size = readlink(fd_path, path, sizeof(path) - 1);
                free(fd_path);
                if (size < 0) { path[0] = 0; ++invalid; }
                else path[size] = 0;
                if (uid < 0) ++unresolved;
                /* Do not discard unknown identities or non-desktop events. */
                printf("{\"kind\":\"file_event\",\"pid\":%d,\"uid\":", event->pid);
                if (uid < 0) printf("null"); else printf("%d", uid);
                printf(",\"identity_method\":\"%s\",\"identity_resolved_at\":%.6f",
                       pidfd_mode?"pidfd_proc_at_event":"proc_at_event",now.tv_sec+now.tv_nsec/1e9);
                printf(",\"open\":%s,\"exec\":%s,\"mask\":%llu,\"path\":",
                       (event->mask & FAN_OPEN) ? "true" : "false",
                       (event->mask & FAN_OPEN_EXEC) ? "true" : "false",
                       (unsigned long long)event->mask);
                json_string(path); puts("}");
                ++events;
                close(event->fd);
            }
            if (pidfd>=0) close(pidfd);
            event = FAN_EVENT_NEXT(event, length);
        }
        if (length != 0) ++invalid;
        free(buffer);
    }
    close(fan);
    printf("{\"kind\":\"audit_end\",\"events\":%lu,\"unresolved_uid\":%lu,"
           "\"overflow\":%lu,\"invalid\":%lu}\n", events, unresolved, overflow, invalid);
    return (overflow || invalid) ? EXIT_FAILURE : EXIT_SUCCESS;
}

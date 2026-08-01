/* Narrow setuid-root launcher. Build/install is external to this source tree.
 * It accepts exactly one opaque lower-case 64-hex grant id and no environment.
 */
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
  if (argc != 2 || strlen(argv[1]) != 64) return 64;
  for (size_t i=0; i<64; ++i) if (!((argv[1][i]>='0'&&argv[1][i]<='9')||(argv[1][i]>='a'&&argv[1][i]<='f'))) return 64;
  if (geteuid()!=0) return 77;
  char unit[128];
  int n=snprintf(unit,sizeof unit,"hermes-real-executor@%s.service",argv[1]);
  if (n<0 || n >= (int)sizeof unit) return 64;
  clearenv();
  char *const env[]={(char *)"PATH=/usr/sbin:/usr/bin:/sbin:/bin",(char *)"LANG=C",(char *)"LC_ALL=C",NULL};
  char *const child[]={(char *)"/usr/bin/systemctl",(char *)"start",unit,NULL};
  execve(child[0],child,env); return 127;
}

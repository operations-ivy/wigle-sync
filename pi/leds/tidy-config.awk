# config.txt without the lines an earlier setup-leds.sh added (OLD, one per
# line) and the "wigle-sync: RGB status LEDs" comment above each; also drops
# any such comment left over a removed line by a version that kept them.
#
#   OLD="$(printf '%s\n' "${old_lines[@]}")" awk -v MARK="$marker" -f tidy-config.awk config.txt
BEGIN {
    n = split(ENVIRON["OLD"], list, "\n")
    for (i = 1; i <= n; i++)
        if (list[i] != "")
            old[list[i]] = 1
}
{ line[NR] = $0 }
END {
    for (i = 1; i <= NR; i++) {
        if (line[i] in old)
            continue
        if (line[i] == MARK && (i == NR || line[i + 1] == MARK || (line[i + 1] in old)))
            continue
        print line[i]
    }
}

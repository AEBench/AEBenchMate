#!/usr/bin/env perl
use strict;
use warnings;

while (<>) {
    s/benchconductor/
        $& eq uc($&)      ? 'BENCHMATE' :
        $& eq lc($&)      ? 'benchmate' :
        $& =~ /^[A-Z]/    ? 'Benchmate' :
                            'benchmate'
    /ige;
    print;
}

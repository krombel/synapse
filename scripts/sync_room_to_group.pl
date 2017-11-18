#!/usr/bin/env perl

use strict;
use warnings;

use JSON::XS;
use LWP::UserAgent;
use URI::Escape;

if (@ARGV < 4) {
    die "usage: $0 <homeserver url> <access_token> <room_id|room_alias> <group_id>\n";
}

my ($hs, $access_token, $room_id, $group_id) = @ARGV;
my $ua = LWP::UserAgent->new();
$ua->timeout(10);

if ($room_id =~ /^#/) {
    $room_id = uri_escape($room_id);
    $room_id = decode_json($ua->get("${hs}/_matrix/client/r0/directory/room/${room_id}?access_token=${access_token}")->decoded_content)->{room_id};
}

my $room_users  = [ keys %{decode_json($ua->get("${hs}/_matrix/client/r0/rooms/${room_id}/joined_members?access_token=${access_token}")->decoded_content)->{joined}} ];
my $group_users = [ map { $_->{user_id} } @{decode_json($ua->get("${hs}/_matrix/client/unstable/groups/${group_id}/users?access_token=${access_token}" )->decoded_content)->{chunk}} ];

my $diff = {};
foreach my $user (@$room_users) { $diff->{$user}++ }
foreach my $user (@$group_users) { $diff->{$user}-- }

foreach my $user (keys %$diff) {
    if ($diff->{$user} == 1) {
        warn "inviting $user";
        $ua->put("${hs}/_matrix/client/unstable/groups/${group_id}/admin/users/invite/${user}?access_token=${access_token}", Content=>'{}');
    }
    elsif ($diff->{$user} == -1) {
        warn "removing $user";
        $ua->put("${hs}/_matrix/client/unstable/groups/${group_id}/admin/users/remove/${user}?access_token=${access_token}", Content=>'{}');
    }
}
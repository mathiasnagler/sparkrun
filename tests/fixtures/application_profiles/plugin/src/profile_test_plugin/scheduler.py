"""Installed scheduler exercising the public scheduler extension contract."""

from sparkrun.core.scheduler import RankAssignment, RankSlot, Scheduler, SchedulingResult


class ProfileTestScheduler(Scheduler):
    scheduler_name = "profile-test"

    def schedule(self, request):
        # The fixture only schedules a single rank on one host.
        assert request.parallelism.world_size() == 1 and len(request.hosts) == 1
        return SchedulingResult(
            assignment=RankAssignment(by_rank=(RankSlot(request.hosts[0], 0),), hosts_used=request.hosts),
            scheduler_name=self.scheduler_name,
        )

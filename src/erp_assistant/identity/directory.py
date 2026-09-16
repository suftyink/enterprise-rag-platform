from dataclasses import dataclass

from erp_assistant.identity.models import IdentityMembership


@dataclass(frozen=True)
class StaticIdentityDirectory:
    memberships: dict[str, IdentityMembership]

    async def resolve(self, subject_id: str) -> IdentityMembership | None:
        return self.memberships.get(subject_id)

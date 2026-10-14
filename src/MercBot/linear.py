from MercBot.models import PhotoIssue

# Placeholder data until the Linear GraphQL integration is built. Titles arrive
# already stripped of the "Photos - " prefix; the real client should do the same.
MOCK_ISSUES = [
    PhotoIssue(title="Homecoming game"),
    PhotoIssue(title="New faculty headshots"),
    PhotoIssue(title="Budget explainer infographic"),
]


class LinearClient:
    async def fetch_unassigned_photo_issues(self) -> list[PhotoIssue]:
        return list(MOCK_ISSUES)

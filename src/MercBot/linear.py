from datetime import date

from MercBot.models import PhotoIssue

# Placeholder data until the Linear GraphQL integration is built.
MOCK_ISSUES = [
    PhotoIssue(
        title="Homecoming game -photo",
        url="https://linear.app/example/issue/MER-101/homecoming-game-photo",
        due_date=date(2026, 10, 18),
    ),
    PhotoIssue(
        title="New faculty headshots -photo",
        url="https://linear.app/example/issue/MER-104/new-faculty-headshots-photo",
    ),
    PhotoIssue(
        title="Budget explainer infographic -photo",
        url="https://linear.app/example/issue/MER-109/budget-explainer-infographic-photo",
        due_date=date(2026, 10, 21),
    ),
]


class LinearClient:
    async def fetch_unassigned_photo_issues(self) -> list[PhotoIssue]:
        return list(MOCK_ISSUES)

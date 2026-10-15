ISSUE_LABELS = """
query IssueLabels($after: String) {
  issueLabels(first: 250, after: $after) {
    nodes {
      name
      isGroup
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

UNASSIGNED_ISSUES_WITH_LABEL = """
query UnassignedIssuesWithLabel($label: String!, $after: String) {
  issues(
    first: 100
    after: $after
    filter: {
      labels: { some: { name: { eqIgnoreCase: $label } } }
      assignee: { null: true }
      state: { type: { nin: ["completed", "canceled"] } }
    }
  ) {
    nodes {
      title
      parent {
        title
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

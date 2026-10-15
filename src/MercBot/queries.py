ISSUE_LABELS = """
query IssueLabels($after: String) {
  issueLabels(first: 250, after: $after) {
    nodes {
      id
      name
      isGroup
      team {
        key
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

WORKSPACE = """
query Workspace {
  teams(first: 50) {
    nodes {
      id
      key
    }
  }
  projects(first: 100) {
    nodes {
      id
      name
      state
      targetDate
      teams(first: 20) {
        nodes {
          key
        }
      }
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

CREATE_ISSUE = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      id
      identifier
      url
    }
  }
}
"""

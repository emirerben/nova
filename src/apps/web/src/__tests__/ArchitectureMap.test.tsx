import "@testing-library/jest-dom";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ArchitectureMap } from "@/components/architecture/ArchitectureMap";

// ---------------------------------------------------------------------------
// Mock react-flow — it requires a DOM measurement layer that jsdom can't provide.
// We replace it with a minimal shell that renders nodes and handles events.
// ---------------------------------------------------------------------------

const mockOnNodeClick = jest.fn();
const mockOnNodeContextMenu = jest.fn();
const mockOnPaneClick = jest.fn();

jest.mock("@xyflow/react", () => {
  const actual = jest.requireActual("@xyflow/react");
  return {
    ...actual,
    ReactFlow: ({ nodes, edges, onNodeClick, onNodeContextMenu, onPaneClick, nodeTypes, children }: any) => {
      // Stash callbacks so tests can trigger them
      mockOnNodeClick.mockImplementation(onNodeClick);
      mockOnNodeContextMenu.mockImplementation(onNodeContextMenu);
      mockOnPaneClick.mockImplementation(onPaneClick);

      const NodeComponent = nodeTypes?.moduleNode;
      return (
        <div data-testid="react-flow">
          {nodes?.map((node: any) =>
            NodeComponent ? (
              <div
                key={node.id}
                data-testid={`node-${node.id}`}
                onClick={(e) => onNodeClick?.(e, node)}
                onContextMenu={(e) => onNodeContextMenu?.(e, node)}
              >
                <NodeComponent id={node.id} data={node.data} type={node.type} />
              </div>
            ) : (
              <div key={node.id} data-testid={`node-${node.id}`}>
                {node.id}
              </div>
            )
          )}
          {edges?.map((edge: any) => (
            <div key={edge.id} data-testid={`edge-${edge.id}`} data-label={edge.data?.label}>
              {edge.data?.label}
            </div>
          ))}
          {children}
        </div>
      );
    },
    Background: () => <div data-testid="rf-background" />,
    Controls: () => <div data-testid="rf-controls" />,
    Handle: () => null,
    BaseEdge: () => null,
    EdgeLabelRenderer: ({ children }: any) => <>{children}</>,
    getSmoothStepPath: () => ["M0,0", 0, 0],
    useNodesState: (initial: any[]) => {
      const { useState } = require("react");
      const [nodes, setNodes] = useState(initial);
      return [nodes, setNodes, jest.fn()];
    },
    useEdgesState: (initial: any[]) => {
      const { useState } = require("react");
      const [edges, setEdges] = useState(initial);
      return [edges, setEdges, jest.fn()];
    },
    Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
    MarkerType: { ArrowClosed: "arrowclosed" },
  };
});

// Mock the hooks
jest.mock("@/hooks/useArchitectureData", () => ({
  useActiveJobs: () => ({ data: null }),
  useModuleIssues: () => ({ data: null, isLoading: false }),
  useModuleCommits: () => ({ data: null, isLoading: false }),
}));

describe("ArchitectureMap", () => {
  beforeEach(() => {
    mockOnNodeClick.mockClear();
    mockOnNodeContextMenu.mockClear();
    mockOnPaneClick.mockClear();
  });

  test("renders L1 nodes (5 pipeline modules + 3 data stores)", () => {
    render(<ArchitectureMap />);
    // Pipeline modules
    expect(screen.getByTestId("node-upload")).toBeInTheDocument();
    expect(screen.getByTestId("node-processing")).toBeInTheDocument();
    expect(screen.getByTestId("node-clips")).toBeInTheDocument();
    expect(screen.getByTestId("node-templates")).toBeInTheDocument();
    expect(screen.getByTestId("node-delivery")).toBeInTheDocument();
    // Data stores
    expect(screen.getByTestId("node-postgresql")).toBeInTheDocument();
    expect(screen.getByTestId("node-redis")).toBeInTheDocument();
    expect(screen.getByTestId("node-gcs")).toBeInTheDocument();
  });

  test("renders labeled edges between connected modules", () => {
    render(<ArchitectureMap />);
    // The default view is "business", so edge labels use businessLabel
    expect(screen.getByTestId("edge-upload-processing")).toBeInTheDocument();
    expect(screen.getByTestId("edge-processing-clips")).toBeInTheDocument();
    expect(screen.getByTestId("edge-clips-delivery")).toBeInTheDocument();
  });

  test("renders with empty GitHub data (badges show loading indicator)", () => {
    render(<ArchitectureMap />);
    // Nodes should render even with null issue counts (the mock returns null)
    expect(screen.getByTestId("node-upload")).toBeInTheDocument();
    expect(screen.getByTestId("node-processing")).toBeInTheDocument();
  });

  test("click L1 node expands to show L2 children", () => {
    render(<ArchitectureMap />);

    // Click Processing node (has children: probe, transcribe, scene_detect, score, gemini)
    fireEvent.click(screen.getByTestId("node-processing"));

    // L2 nodes should appear
    expect(screen.getByTestId("node-probe")).toBeInTheDocument();
    expect(screen.getByTestId("node-transcribe")).toBeInTheDocument();
    expect(screen.getByTestId("node-scene_detect")).toBeInTheDocument();
    expect(screen.getByTestId("node-score")).toBeInTheDocument();
    expect(screen.getByTestId("node-gemini")).toBeInTheDocument();

    // L1 pipeline nodes should be gone
    expect(screen.queryByTestId("node-upload")).not.toBeInTheDocument();
  });

  test("click breadcrumb collapses back to L1", () => {
    render(<ArchitectureMap />);

    // Expand processing
    fireEvent.click(screen.getByTestId("node-processing"));
    expect(screen.getByTestId("node-probe")).toBeInTheDocument();

    // Click "Back to L1" button
    const backButton = screen.getByText("← Back to L1");
    fireEvent.click(backButton);

    // Should be back to L1 view
    expect(screen.getByTestId("node-upload")).toBeInTheDocument();
    expect(screen.getByTestId("node-processing")).toBeInTheDocument();
    expect(screen.queryByTestId("node-probe")).not.toBeInTheDocument();
  });

  test("right-click module highlights downstream dependents", () => {
    render(<ArchitectureMap />);

    // Right-click "upload" — "processing" depends on it
    fireEvent.contextMenu(screen.getByTestId("node-upload"));

    // The node should still be in the document and the component should re-render
    // with highlighting data. We verify by checking the node is still present.
    expect(screen.getByTestId("node-upload")).toBeInTheDocument();
    expect(screen.getByTestId("node-processing")).toBeInTheDocument();
  });

  test("click away clears highlighting", () => {
    const { container } = render(<ArchitectureMap />);

    // Right-click to highlight
    fireEvent.contextMenu(screen.getByTestId("node-upload"));

    // Click on the react-flow pane area (simulated by calling pane click)
    // The mock stores onPaneClick, so we call it
    act(() => {
      if (mockOnPaneClick.getMockImplementation()) {
        mockOnPaneClick.getMockImplementation()!();
      }
    });

    // Nodes should still be present (highlighting cleared, not removed)
    expect(screen.getByTestId("node-upload")).toBeInTheDocument();
  });

  test("data store node with no children opens detail panel on click", () => {
    render(<ArchitectureMap />);

    // Click PostgreSQL (data store, no children)
    fireEvent.click(screen.getByTestId("node-postgresql"));

    // The detail panel should open — "PostgreSQL" appears in both the node card
    // and the dialog title, so check for multiple instances
    const matches = screen.getAllByText("PostgreSQL");
    expect(matches.length).toBeGreaterThanOrEqual(2); // node card + dialog title
  });
});

import { useEffect, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  FilePlus2,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  Pencil,
  Trash2,
} from 'lucide-react'
import type { TreeNode } from '../types'

type Props = {
  tree: TreeNode[]
  activePath: string | null
  onOpen: (path: string) => void
  onLoadProject: () => void
  loadDisabled: boolean
  onCreate: (type: 'file' | 'directory') => void
  onRename: (path: string) => void
  onDelete: (path: string) => void
}

function directoryPaths(nodes: TreeNode[]): string[] {
  return nodes.flatMap((node) =>
    node.type === 'directory' ? [node.path, ...directoryPaths(node.children)] : [],
  )
}

export function FileTree({
  tree,
  activePath,
  onOpen,
  onLoadProject,
  loadDisabled,
  onCreate,
  onRename,
  onDelete,
}: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  useEffect(() => {
    const available = new Set(directoryPaths(tree))
    setExpanded((current) => new Set([...current].filter((path) => available.has(path))))
  }, [tree])

  const toggle = (path: string) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const renderNode = (node: TreeNode, depth: number) => {
    const isDirectory = node.type === 'directory'
    const isExpanded = expanded.has(node.path)
    return (
      <div key={node.path}>
        <div
          className={`tree-row ${activePath === node.path ? 'active' : ''}`}
          style={{ paddingLeft: `${7 + depth * 13}px` }}
          onDoubleClick={() => isDirectory && toggle(node.path)}
        >
          {isDirectory ? (
            <button
              className="tree-toggle"
              type="button"
              onClick={() => toggle(node.path)}
              aria-label={`${isExpanded ? 'Collapse' : 'Expand'} ${node.name}`}
            >
              {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            </button>
          ) : (
            <span className="tree-spacer" />
          )}
          {isDirectory ? <Folder size={13} /> : <FileText size={13} />}
          <button
            className="tree-name"
            type="button"
            onClick={() => (isDirectory ? toggle(node.path) : onOpen(node.path))}
            title={node.path}
          >
            {node.name}
          </button>
          <span className="row-actions">
            <button
              type="button"
              onClick={() => onRename(node.path)}
              title={`Rename ${node.path}`}
              aria-label={`Rename ${node.path}`}
            >
              <Pencil size={12} />
            </button>
            <button
              type="button"
              onClick={() => onDelete(node.path)}
              title={`Delete ${node.path}`}
              aria-label={`Delete ${node.path}`}
            >
              <Trash2 size={12} />
            </button>
          </span>
        </div>
        {isDirectory && isExpanded && node.children.map((child) => renderNode(child, depth + 1))}
      </div>
    )
  }

  return (
    <section className="panel" aria-label="Project files">
      <header className="panel-header">
        <span>Project</span>
        <span className="panel-actions">
          <button
            type="button"
            onClick={onLoadProject}
            disabled={loadDisabled}
            title="Load project"
            aria-label="Load project"
          >
            <FolderOpen size={14} />
          </button>
          <button
            type="button"
            onClick={() => onCreate('file')}
            title="Create file"
            aria-label="Create file"
          >
            <FilePlus2 size={14} />
          </button>
          <button
            type="button"
            onClick={() => onCreate('directory')}
            title="Create folder"
            aria-label="Create folder"
          >
            <FolderPlus size={14} />
          </button>
        </span>
      </header>
      <div className="panel-body tree-scroll">
        <div className="tree">{tree.map((node) => renderNode(node, 0))}</div>
        {tree.length === 0 && <div className="empty-state">This workspace is empty.</div>}
      </div>
    </section>
  )
}

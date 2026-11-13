import Foundation

/// Which bytes a file holds, as far as the filesystem can say without reading them: the volume, the inode, the size and the
/// modification time to the nanosecond.
///
/// A hardlink is a second name for the same inode, so it has the identity of the file it links to, while a copy (a new
/// inode) never does. That is the point: work done on one name (a hash, a proxy transcode) may be reused for the other only
/// when their identities are equal. Change time is left out on purpose, because linking a file bumps it.
public struct FileIdentity: Hashable, Sendable {
    public let device: UInt64
    public let inode: UInt64
    public let size: Int64
    public let modifiedSeconds: Int64
    public let modifiedNanoseconds: Int64

    public init(of url: URL) throws {
        var info = stat()
        let status = url.withUnsafeFileSystemRepresentation { path -> Int32 in
            guard let path else { return -1 }
            return stat(path, &info)
        }
        guard status == 0 else { throw CocoaError(.fileReadNoSuchFile) }
        device = UInt64(truncatingIfNeeded: info.st_dev)
        inode = UInt64(info.st_ino)
        size = Int64(info.st_size)
        modifiedSeconds = Int64(info.st_mtimespec.tv_sec)
        modifiedNanoseconds = Int64(info.st_mtimespec.tv_nsec)
    }
}

/// A digest and the identity of the file it was computed from.
public struct KnownFingerprint: Sendable, Equatable {
    public let identity: FileIdentity
    public let fingerprint: AssetFingerprint
    public init(identity: FileIdentity, fingerprint: AssetFingerprint) {
        self.identity = identity; self.fingerprint = fingerprint
    }
}

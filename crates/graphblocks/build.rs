fn main() {
    let notice = include_str!("RESERVED_PACKAGE_NOTICE.txt").trim();
    println!("cargo:rerun-if-changed=RESERVED_PACKAGE_NOTICE.txt");
    println!("cargo:warning={notice}");
}

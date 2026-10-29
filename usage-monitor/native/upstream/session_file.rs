use std::{
    fs::File,
    io::{self, BufRead, BufReader, Read},
    path::Path,
};

pub(crate) fn is_session_file(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".jsonl") || name.ends_with(".jsonl.zst"))
}

pub(crate) fn open_session_file(path: &Path) -> io::Result<Box<dyn BufRead>> {
    let file = File::open(path)?;
    if path.extension().is_some_and(|extension| extension == "zst") {
        let decoder = zstd::stream::read::Decoder::new(file)?;
        Ok(Box::new(BufReader::new(decoder)))
    } else {
        Ok(Box::new(BufReader::new(file)))
    }
}

pub(crate) fn read_session_file(path: &Path) -> Result<String, String> {
    let mut content = String::new();
    let result = open_session_file(path).and_then(|mut reader| reader.read_to_string(&mut content));
    result.map_err(|error| format!("Failed to read session {}: {error}", path.display()))?;
    Ok(content)
}

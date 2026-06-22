pub type Finalizer = Box<dyn FnOnce() + Send + 'static>;

#[derive(Debug, Eq, PartialEq)]
pub enum ResourceError {
    ScopeClosed { scope_id: String },
}

pub struct ResourceScope {
    id: String,
    closed: bool,
    finalizers: Vec<Finalizer>,
}

impl ResourceScope {
    pub fn new(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            closed: false,
            finalizers: Vec::new(),
        }
    }

    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn is_closed(&self) -> bool {
        self.closed
    }

    pub fn defer(&mut self, finalizer: impl FnOnce() + Send + 'static) {
        if self.try_defer(finalizer).is_err() {
            debug_assert!(self.closed);
        }
    }

    pub fn try_defer(
        &mut self,
        finalizer: impl FnOnce() + Send + 'static,
    ) -> Result<(), ResourceError> {
        if self.closed {
            return Err(ResourceError::ScopeClosed {
                scope_id: self.id.clone(),
            });
        }
        self.finalizers.push(Box::new(finalizer));
        Ok(())
    }

    pub fn close(&mut self) -> bool {
        if self.closed {
            return false;
        }
        self.closed = true;
        while let Some(finalizer) = self.finalizers.pop() {
            finalizer();
        }
        true
    }
}

impl Drop for ResourceScope {
    fn drop(&mut self) {
        self.close();
    }
}
